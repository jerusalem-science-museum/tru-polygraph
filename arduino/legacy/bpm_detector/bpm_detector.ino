// Robust BPM detector for MAX30102 / MAX30105 (SparkFun MAX3010x library).
//
// Pipeline (all at the sensor's fixed 100 Hz sample rate, timed by sample count):
//   raw IR -> band-pass 0.7-3.5 Hz (biquad HP + LP) -> invert (pulse = peak)
//          -> adaptive threshold peak detector with refractory period
//          -> interval (IBI) validation against median of recent IBIs
//          -> BPM = 60000 / median(IBI history)
//
// Serial commands (115200 baud):  t = text diagnostics (default)
//                                 p = Serial Plotter (ac, thr, beat per sample)
//                                 q = quiet, only "BPM=<n>" once per second

#include <Wire.h>
#include "MAX30105.h"

MAX30105 sensor;

// ---- sensor config: 400 Hz / 4-sample average = 100 Hz effective ----
const byte LED_BRIGHTNESS = 60;      // 0-255 (255 = 50 mA); lower if SATURATED shows
const byte SAMPLE_AVERAGE = 4;
const byte LED_MODE       = 2;       // red + IR
const int  SAMPLE_RATE    = 400;
const int  PULSE_WIDTH    = 411;     // 18-bit resolution
const int  ADC_RANGE      = 16384;
const float FS            = 100.0;   // effective samples per second
const uint32_t SAMPLE_MS  = 10;

// ---- detection tuning ----
const uint32_t FINGER_THRESHOLD = 50000;   // raw IR below this = no finger
const uint32_t SATURATION_LEVEL = 250000;  // 18-bit ADC max is 262143
const int      SETTLE_SAMPLES   = 150;     // 1.5 s for filters to settle after placement
const uint32_t MIN_IBI          = 333;     // 180 BPM
const uint32_t MAX_IBI          = 1500;    // 40 BPM
const uint32_t REFRACTORY_MS    = 300;     // never two beats closer than this
const float    THRESH_FRAC      = 0.5;     // threshold = fraction of envelope
const float    ENV_DECAY        = 0.995;   // per sample (~halves every 1.4 s)
const float    ENV_ATTACK       = 0.3;     // partial attack so 1-2 sample spikes don't blow up the envelope
const float    OUTLIER_FRAC     = 0.30;    // reject IBI more than 30% from reference
const byte     IBI_HIST         = 8;       // BPM = median of this many IBIs
const byte     MIN_BEATS_LOCK   = 4;       // accepted IBIs before reporting BPM
const byte     MAX_CONSEC_REJECT = 4;      // then assume we locked onto garbage and re-acquire
const uint32_t BEAT_TIMEOUT_MS  = 3000;    // no accepted beat this long -> re-acquire

// ---- biquad filter (RBJ cookbook, Direct Form I) ----
struct Biquad {
  float b0, b1, b2, a1, a2;
  float x1, x2, y1, y2;

  void design(bool highpass, float fc, float q) {
    float w0 = 2.0 * PI * fc / FS;
    float c = cos(w0), alpha = sin(w0) / (2.0 * q);
    float a0 = 1.0 + alpha;
    if (highpass) { b0 = (1.0 + c) / 2.0; b1 = -(1.0 + c); }
    else          { b0 = (1.0 - c) / 2.0; b1 =  (1.0 - c); }
    b2 = b0;
    b0 /= a0; b1 /= a0; b2 /= a0;
    a1 = -2.0 * c / a0;
    a2 = (1.0 - alpha) / a0;
  }
  void reset(float x, float y) { x1 = x2 = x; y1 = y2 = y; }
  float step(float x) {
    float y = b0 * x + b1 * x1 + b2 * x2 - a1 * y1 - a2 * y2;
    x2 = x1; x1 = x; y2 = y1; y1 = y;
    return y;
  }
};

Biquad hpf, lpf;

enum State : byte { NO_FINGER, SETTLING, ACQUIRING, LOCKED };
const char* STATE_NAMES[] = { "NO_FINGER", "SETTLING", "ACQUIRING", "LOCKED" };
enum Mode : byte { MODE_TEXT, MODE_PLOT, MODE_QUIET };

State state = NO_FINGER;
Mode mode = MODE_TEXT;

// signal state
uint32_t sampleCount = 0;      // time base: t = sampleCount * SAMPLE_MS
uint32_t lastRaw = 0;
float dc = 0;                  // slow average of raw IR (for perfusion index)
float sig = 0, sig1 = 0, sig2 = 0;
float env = 0;                 // peak envelope of filtered signal
int settleLeft = 0;

// beat state
uint32_t lastPeakT = 0;
uint32_t lastAcceptT = 0;
uint16_t ibiHist[IBI_HIST];
byte ibiSpot = 0, ibiCount = 0;
uint16_t lastIbi = 0;
byte consecReject = 0;
int bpm = 0;

// diagnostics counters
uint32_t acceptedTotal = 0, rejectedTotal = 0;
uint32_t samplesThisSec = 0;
unsigned long lastStatus = 0;
bool beatThisSample = false;

uint16_t medianIbi() {
  uint16_t tmp[IBI_HIST];
  for (byte i = 0; i < ibiCount; i++) tmp[i] = ibiHist[i];
  for (byte i = 1; i < ibiCount; i++) {          // insertion sort, n <= 8
    uint16_t v = tmp[i];
    int8_t j = i - 1;
    while (j >= 0 && tmp[j] > v) { tmp[j + 1] = tmp[j]; j--; }
    tmp[j + 1] = v;
  }
  if (ibiCount % 2) return tmp[ibiCount / 2];
  return (tmp[ibiCount / 2 - 1] + tmp[ibiCount / 2]) / 2;
}

void clearBeats() {
  ibiSpot = ibiCount = 0;
  lastIbi = 0;
  consecReject = 0;
  bpm = 0;
}

void pushIbi(uint16_t ibi) {
  ibiHist[ibiSpot] = ibi;
  ibiSpot = (ibiSpot + 1) % IBI_HIST;
  if (ibiCount < IBI_HIST) ibiCount++;
  lastIbi = ibi;
}

void logBeat(uint32_t t, uint32_t ibi, uint16_t ref, const char* verdict) {
  if (mode != MODE_TEXT) return;
  Serial.print(F("BEAT t=")); Serial.print(t);                         // time of the peak, ms since boot (from sample count)
  Serial.print(F(" ibi=")); Serial.print(ibi);                          // interval since previous beat, ms (0 = first beat)
  Serial.print(F(" inst=")); Serial.print(ibi ? 60000.0 / ibi : 0, 1);  // instantaneous BPM from this one interval
  Serial.print(F(" ref=")); Serial.print(ref);                          // interval it was compared to, ms (median, or last IBI while acquiring; 0 = none)
  Serial.print(F(" peak=")); Serial.print(sig1, 0);                     // filtered signal height at the peak
  Serial.print(F(" thr=")); Serial.print(env * THRESH_FRAC, 0);         // detection threshold at that moment
  Serial.print(F(" -> ")); Serial.print(verdict);                       // ok / first / REJECT early|late|too slow
  Serial.print(F(" | bpm=")); Serial.print(bpm);                        // reported BPM after this beat (0 until locked)
  Serial.print(F(" n=")); Serial.println(ibiCount);                     // intervals in the median history (max IBI_HIST)
}

void handlePeak(uint32_t tPeak) {
  beatThisSample = true;

  if (lastPeakT == 0) {                          // first peak: nothing to measure yet
    lastPeakT = tPeak;
    logBeat(tPeak, 0, 0, "first");
    return;
  }

  uint32_t ibi = tPeak - lastPeakT;
  uint16_t ref = (ibiCount >= 3) ? medianIbi() : lastIbi;

  if (ibi > MAX_IBI) {                           // missed beats / pause: restart the chain
    lastPeakT = tPeak;
    rejectedTotal++;
    logBeat(tPeak, ibi, ref, "REJECT too slow");
    return;
  }

  if (ibi < MIN_IBI || (ref && ibi < ref * (1.0 - OUTLIER_FRAC))) {
    // Extra peak (noise / dicrotic notch). Keep lastPeakT so the next real
    // beat is still measured from the previous real beat.
    rejectedTotal++;
    consecReject++;
    logBeat(tPeak, ibi, ref, "REJECT early");
  } else if (ref && ibi > ref * (1.0 + OUTLIER_FRAC)) {
    lastPeakT = tPeak;                           // probably a missed beat
    rejectedTotal++;
    consecReject++;
    logBeat(tPeak, ibi, ref, "REJECT late");
  } else {
    lastPeakT = tPeak;
    lastAcceptT = tPeak;
    pushIbi(ibi);
    consecReject = 0;
    acceptedTotal++;
    if (ibiCount >= MIN_BEATS_LOCK) {
      state = LOCKED;
      bpm = (int)(60000.0 / medianIbi() + 0.5);
    }
    logBeat(tPeak, ibi, ref, "ok");
    return;
  }

  // While acquiring, an inconsistent interval just reseeds the history.
  if (ibiCount < 3 && ibi >= MIN_IBI) {
    clearBeats();
    pushIbi(ibi);
    lastPeakT = tPeak;
    lastAcceptT = tPeak;
  } else if (consecReject >= MAX_CONSEC_REJECT) {
    clearBeats();
    state = ACQUIRING;
    if (mode == MODE_TEXT) Serial.println(F("** too many rejects, re-acquiring")); // history dropped, BPM back to 0
  }
}

void processSample(uint32_t raw) {
  sampleCount++;
  samplesThisSec++;
  lastRaw = raw;
  beatThisSample = false;
  uint32_t t = sampleCount * SAMPLE_MS;

  if (raw < FINGER_THRESHOLD) {
    if (state != NO_FINGER) {
      state = NO_FINGER;
      clearBeats();
      lastPeakT = 0;
      env = sig = sig1 = sig2 = 0;
    }
    return;
  }

  if (state == NO_FINGER) {                      // finger just placed
    state = SETTLING;
    settleLeft = SETTLE_SAMPLES;
    hpf.reset(raw, 0);                           // avoid a huge DC step transient
    lpf.reset(0, 0);
    dc = raw;
    env = 0;
  }

  dc += 0.01 * ((float)raw - dc);
  sig2 = sig1;
  sig1 = sig;
  sig = -lpf.step(hpf.step((float)raw));        // IR dips on each pulse -> invert

  if (state == SETTLING) {
    if (--settleLeft <= 0) {
      state = ACQUIRING;
      lastAcceptT = t;
    }
    return;
  }

  // envelope: fast-ish attack, slow decay
  env *= ENV_DECAY;
  if (sig > env) env += ENV_ATTACK * (sig - env);
  float thr = env * THRESH_FRAC;

  // local maximum at previous sample, above threshold, outside refractory window
  uint32_t tPrev = t - SAMPLE_MS;
  if (sig1 > sig2 && sig1 >= sig && sig1 > thr && sig1 > 0 &&
      (lastPeakT == 0 || tPrev - lastPeakT >= REFRACTORY_MS)) {
    handlePeak(tPrev);
  }

  if (ibiCount > 0 && t - lastAcceptT > BEAT_TIMEOUT_MS) {
    clearBeats();
    lastAcceptT = t;
    state = ACQUIRING;
    if (mode == MODE_TEXT) Serial.println(F("** beat timeout, re-acquiring"));    // no accepted beat for BEAT_TIMEOUT_MS
  }
}

void printPlot() {
  Serial.print(F("ac:"));    Serial.print(sig, 1);                         // band-passed, inverted pulse signal
  Serial.print(F(",thr:"));  Serial.print(env * THRESH_FRAC, 1);           // adaptive threshold a peak must cross
  Serial.print(F(",beat:")); Serial.println(beatThisSample ? env : 0, 1);  // spike on samples where a peak was detected
}

void printStatus() {
  if (mode == MODE_QUIET) {
    Serial.print(F("BPM="));
    Serial.println(state == LOCKED ? bpm : 0);                           // final BPM, 0 when not locked
    return;
  }
  Serial.print(F("STATUS ")); Serial.print(STATE_NAMES[state]);          // NO_FINGER / SETTLING / ACQUIRING / LOCKED
  Serial.print(F(" bpm=")); Serial.print(bpm);                           // reported BPM (median of recent intervals)
  Serial.print(F(" raw=")); Serial.print(lastRaw);                       // latest raw IR reading (18-bit ADC counts)
  Serial.print(F(" dc=")); Serial.print(dc, 0);                          // slow average of raw IR (steady light level)
  Serial.print(F(" ac=")); Serial.print(env, 1);                         // pulse amplitude (envelope of filtered signal)
  Serial.print(F(" pi=")); Serial.print(dc > 0 ? 100.0 * env / dc : 0, 3); // perfusion index: pulse strength as % of light level
  Serial.print(F("% sps=")); Serial.print(samplesThisSec);               // samples processed in the last second (expect ~100)
  Serial.print(F(" ok=")); Serial.print(acceptedTotal);                  // total accepted beats since boot
  Serial.print(F(" rej=")); Serial.print(rejectedTotal);                 // total rejected beats since boot
  if (lastRaw > SATURATION_LEVEL) Serial.print(F(" SATURATED(lower LED_BRIGHTNESS)")); // sensor maxed out, no pulse visible
  Serial.println();
}

void handleSerial() {
  while (Serial.available()) {
    char c = Serial.read();
    if (c == 't') mode = MODE_TEXT;
    else if (c == 'p') mode = MODE_PLOT;
    else if (c == 'q') mode = MODE_QUIET;
  }
}

void setup() {
  Serial.begin(115200);

  if (!sensor.begin(Wire, I2C_SPEED_FAST)) {
    Serial.println(F("MAX3010x not found. Check wiring."));
    while (1);
  }

  sensor.setup(LED_BRIGHTNESS, SAMPLE_AVERAGE, LED_MODE, SAMPLE_RATE, PULSE_WIDTH, ADC_RANGE);
  sensor.setPulseAmplitudeRed(0x0A);            // red unused for BPM; keep it dim

  hpf.design(true, 0.7, 0.7071);                // removes DC + breathing drift
  lpf.design(false, 3.5, 0.7071);               // removes noise / dicrotic detail

  Serial.println(F("BPM detector ready. Commands: t=text p=plot q=quiet"));
}

void loop() {
  handleSerial();

  // Drain every sample the sensor has buffered so none are skipped.
  sensor.check();
  while (sensor.available()) {
    uint32_t ir = sensor.getFIFOIR();
    sensor.nextSample();
    processSample(ir);
    if (mode == MODE_PLOT) printPlot();
  }

  if (millis() - lastStatus >= 1000) {
    lastStatus = millis();
    if (mode != MODE_PLOT) printStatus();
    samplesThisSec = 0;
  }
}
