// Combined polygraph firmware: GSR at 500 Hz on A0 + MAX30102 heart rate (I2C).
//
// Serial protocol (115200 baud, one record per line, see README.md):
//   <t_us>,<raw_adc>            500 Hz GSR sample (t_us = micros() just before analogRead)
//   B,<t_us>,<bpm>,<state>      1 Hz heart rate (bpm 0 unless LOCKED)
//   # <text>                    info / comments
//
// Serial commands: t = test mode (human-readable 1 s summary per sensor, stream off)
//                  s = stream mode (default, what the app reads)
// Test mode is not remembered: opening the port resets the board into stream mode.
//
// The BPM algorithm is ported unchanged from legacy/bpm_detector/bpm_detector.ino
// (sample-count time base, biquad band-pass, adaptive peak detector, IBI validation).
// The GSR tick has priority: the loop never blocks and drains at most one
// MAX30102 sample per iteration.

#include <Wire.h>
#include "MAX30105.h"

MAX30105 sensor;
bool sensorOk = false;

// ---- GSR ----
const byte GSR_PIN = A0;
const uint32_t GSR_INTERVAL_US = 2000;     // 500 Hz
const uint32_t LATE_US   = 500;            // a tick later than this counts as "late"
const uint32_t RESYNC_US = 10000;          // more than this behind -> resync, no catch-up burst
const uint32_t SENSOR_POLL_US = 4000;      // how often to ask the MAX30102 for new FIFO data
// Don't start I2C this close to the next GSR tick. A sensor.check() takes ~0.5 ms.
// Must stay well under 2000 - (GSR tick cost): in stream mode printing the GSR line
// (10-digit uint32 -> ~10 slow divisions) + analogRead uses ~0.6 ms of each 2 ms tick,
// and the old 1500 us guard left no window at all, so the HR sensor was never read.
const uint32_t I2C_GUARD_US   = 700;

uint32_t nextGsrCheck = 0;                 // previousMicros of the original scheduler
uint32_t lastSensorPoll = 0;
uint32_t lateTicks = 0, resyncs = 0;

// ---- test mode (see printTestReport) ----
const int GSR_OPEN_MAX    = 0;             // reads 0 -> electrodes open / finger off
const int GSR_SHORT_MIN   = 1020;          // reads ~5 V -> short or wiring fault
const int GSR_UNSTEADY    = 50;            // min..max spread in 1 s (LSB) that counts as unsteady
const uint32_t IR_SATURATED = 250000;      // 18-bit ADC max is 262143
bool testMode = false;
uint32_t gsrSum = 0;
uint16_t gsrCount = 0;
int gsrMin = 1023, gsrMax = 0;
uint16_t hrCount = 0;
uint32_t lastIr = 0;
uint32_t acceptedTotal = 0, rejectedTotal = 0;
uint16_t beatIbi = 0;                      // set on an accepted beat, printed and cleared in test mode
bool hrLinePending = false;

// ---- sensor config: 400 Hz / 4-sample average = 100 Hz effective ----
const byte LED_BRIGHTNESS = 60;      // 0-255 (255 = 50 mA)
const byte SAMPLE_AVERAGE = 4;
const byte LED_MODE       = 2;       // red + IR
const int  SAMPLE_RATE    = 400;
const int  PULSE_WIDTH    = 411;     // 18-bit resolution
const int  ADC_RANGE      = 16384;
const float FS            = 100.0;   // effective samples per second
const uint32_t SAMPLE_MS  = 10;

// ---- detection tuning ----
const uint32_t FINGER_THRESHOLD = 50000;   // raw IR below this = no finger
const int      SETTLE_SAMPLES   = 150;     // 1.5 s for filters to settle after placement
const uint32_t MIN_IBI          = 333;     // 180 BPM
const uint32_t MAX_IBI          = 1500;    // 40 BPM
const uint32_t REFRACTORY_MS    = 300;     // never two beats closer than this
const float    THRESH_FRAC      = 0.5;     // threshold = fraction of envelope
const float    ENV_DECAY        = 0.995;   // per sample
const float    ENV_ATTACK       = 0.3;     // partial attack so 1-2 sample spikes don't blow up the envelope
const float    OUTLIER_FRAC     = 0.30;    // reject IBI more than 30% from reference
const byte     IBI_HIST         = 8;       // BPM = median of this many IBIs
const byte     MIN_BEATS_LOCK   = 4;       // accepted IBIs before reporting BPM
const byte     MAX_CONSEC_REJECT = 4;      // then re-acquire
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
const char STATE_NAMES[][10] PROGMEM = { "NO_FINGER", "SETTLING", "ACQUIRING", "LOCKED" };

State state = NO_FINGER;

// signal state
uint32_t sampleCount = 0;      // time base: t = sampleCount * SAMPLE_MS
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

void handlePeak(uint32_t tPeak) {
  if (lastPeakT == 0) {                          // first peak: nothing to measure yet
    lastPeakT = tPeak;
    return;
  }

  uint32_t ibi = tPeak - lastPeakT;
  uint16_t ref = (ibiCount >= 3) ? medianIbi() : lastIbi;

  if (ibi > MAX_IBI) {                           // missed beats / pause: restart the chain
    lastPeakT = tPeak;
    rejectedTotal++;
    return;
  }

  if (ibi < MIN_IBI || (ref && ibi < ref * (1.0 - OUTLIER_FRAC))) {
    // Extra peak (noise / dicrotic notch). Keep lastPeakT so the next real
    // beat is still measured from the previous real beat.
    rejectedTotal++;
    consecReject++;
  } else if (ref && ibi > ref * (1.0 + OUTLIER_FRAC)) {
    lastPeakT = tPeak;                           // probably a missed beat
    rejectedTotal++;
    consecReject++;
  } else {
    lastPeakT = tPeak;
    lastAcceptT = tPeak;
    pushIbi(ibi);
    consecReject = 0;
    acceptedTotal++;
    beatIbi = ibi;
    if (ibiCount >= MIN_BEATS_LOCK) {
      state = LOCKED;
      bpm = (int)(60000.0 / medianIbi() + 0.5);
    }
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
  }
}

void processSample(uint32_t raw) {
  sampleCount++;
  hrCount++;
  lastIr = raw;
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
    env = 0;
  }

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
  }
}

void printBpmLine() {
  Serial.print(F("B,"));
  Serial.print(micros());
  Serial.print(',');
  if (!sensorOk) {
    Serial.println(F("0,NO_SENSOR"));
    return;
  }
  Serial.print(state == LOCKED ? bpm : 0);
  Serial.print(',');
  char name[10];
  strcpy_P(name, STATE_NAMES[state]);
  Serial.println(name);
}

void resetTestStats() {
  gsrSum = 0;
  gsrCount = 0;
  gsrMin = 1023;
  gsrMax = 0;
  hrCount = 0;
}

void printStateName() {
  char name[10];
  strcpy_P(name, STATE_NAMES[state]);
  Serial.print(name);
}

// One line per sensor per second, each ending in a plain-language verdict.
void printGsrReport() {
  int avg = gsrCount ? gsrSum / gsrCount : 0;
  Serial.print(F("GSR  raw=")); Serial.print(avg);                       // mean ADC reading this second (0..1023)
  Serial.print(F(" [")); Serial.print(gsrMin);                           // lowest and highest reading this second
  Serial.print(F("..")); Serial.print(gsrMax);
  Serial.print(F("] ")); Serial.print(avg * 5.0 / 1023.0, 2);            // mean voltage on A0
  Serial.print(F(" V  sps=")); Serial.print(gsrCount);                   // samples this second (expect ~500)
  Serial.print(F("  -> "));
  if (gsrCount == 0)                 Serial.println(F("NO SAMPLES (firmware stalled?)"));
  else if (gsrMax <= GSR_OPEN_MAX)   Serial.println(F("NO CONTACT: reads 0 V, check electrodes / fingers"));
  else if (gsrMin >= GSR_SHORT_MIN)  Serial.println(F("MAXED at 5 V: short circuit or wiring fault?"));
  else if (gsrMax - gsrMin > GSR_UNSTEADY) Serial.println(F("UNSTEADY: big swings, hold still / check contact"));
  else                               Serial.println(F("OK"));
}

void printHrReport() {
  Serial.print(F("HR   "));
  if (!sensorOk) {
    Serial.println(F("-> NOT FOUND: check SDA=A4, SCL=A5, VIN, GND (reset after fixing)"));
    return;
  }
  Serial.print(F("ir=")); Serial.print(lastIr);                          // latest raw IR reading (18-bit)
  Serial.print(F(" pulse=")); Serial.print(env, 0);                      // pulse amplitude after filtering
  Serial.print(F(" ")); printStateName();
  Serial.print(F(" bpm=")); Serial.print(state == LOCKED ? bpm : 0);
  Serial.print(F(" sps=")); Serial.print(hrCount);                       // sensor samples this second (expect ~100)
  Serial.print(F(" ok=")); Serial.print(acceptedTotal);                  // accepted / rejected beats since boot
  Serial.print(F(" rej=")); Serial.print(rejectedTotal);
  Serial.print(F("  -> "));
  if (hrCount == 0)                  Serial.println(F("NO SAMPLES from sensor"));
  else if (lastIr > IR_SATURATED)    Serial.println(F("SATURATED: lower LED_BRIGHTNESS or press lighter"));
  else if (state == NO_FINGER)       Serial.println(F("no finger: rest a fingertip lightly on the sensor"));
  else if (state == SETTLING)        Serial.println(F("finger detected, settling..."));
  else if (state == ACQUIRING)       Serial.println(F("looking for steady beats, keep still"));
  else                               Serial.println(F("OK"));
}

void handleSerial() {
  while (Serial.available()) {
    char c = Serial.read();
    if (c == 't' && !testMode) {
      testMode = true;
      resetTestStats();
      Serial.println();
      Serial.println(F("# TEST MODE: one GSR and one HR line per second. Send s to go back to stream mode."));
    } else if (c == 's' && testMode) {
      testMode = false;
      hrLinePending = false;
      Serial.println(F("# stream mode"));
    }
  }
}

void setup() {
  Serial.begin(115200);
  Serial.println(F("# polygraph ready (send t = test mode, s = stream mode)"));

  sensorOk = sensor.begin(Wire, I2C_SPEED_FAST);
  if (sensorOk) {
    sensor.setup(LED_BRIGHTNESS, SAMPLE_AVERAGE, LED_MODE, SAMPLE_RATE, PULSE_WIDTH, ADC_RANGE);
    sensor.setPulseAmplitudeRed(0x0A);          // red unused for BPM; keep it dim
    hpf.design(true, 0.7, 0.7071);              // removes DC + breathing drift
    lpf.design(false, 3.5, 0.7071);             // removes noise / dicrotic detail
  } else {
    Serial.println(F("# MAX3010x not found"));
  }

  nextGsrCheck = micros();
  lastSensorPoll = nextGsrCheck;
}

void loop() {
  static uint32_t lastB = 0, lastDiag = 0;
  uint32_t now = micros();

  // (a) GSR sample when due. Highest priority.
  uint32_t behind = now - nextGsrCheck;
  if (behind >= GSR_INTERVAL_US) {
    uint32_t lag = behind - GSR_INTERVAL_US;
    if (lag > LATE_US) lateTicks++;
    if (lag > RESYNC_US) {                       // long stall: don't burst catch-up samples
      resyncs++;
      nextGsrCheck = now;
    } else {
      nextGsrCheck += GSR_INTERVAL_US;
    }
    uint32_t t = micros();
    int raw = analogRead(GSR_PIN);
    if (testMode) {
      gsrSum += raw;
      gsrCount++;
      if (raw < gsrMin) gsrMin = raw;
      if (raw > gsrMax) gsrMax = raw;
    } else {
      Serial.print(t);
      Serial.print(',');
      Serial.println(raw);
    }
    return;
  }

  // (b) MAX30102: one sample per iteration, never right before a GSR tick.
  if (sensorOk) {
    if (sensor.available()) {
      uint32_t ir = sensor.getFIFOIR();
      sensor.nextSample();
      processSample(ir);
      return;
    }
    if (now - lastSensorPoll >= SENSOR_POLL_US &&
        (GSR_INTERVAL_US - behind) > I2C_GUARD_US) {
      lastSensorPoll = now;
      sensor.check();                            // fills the library's buffer; samples consumed above
      return;
    }
  }

  // (c) 1 Hz heart-rate line and 10 s timing self-check (stream mode),
  //     or the 1 s sensor report (test mode). The two report lines go out on
  //     separate iterations so one print never blocks for long.
  uint32_t ms = millis();
  handleSerial();
  if (testMode) {
    if (beatIbi) {
      Serial.print(F("  beat  ibi=")); Serial.print(beatIbi);
      Serial.print(F(" ms (")); Serial.print(60000 / beatIbi);
      Serial.println(F(" bpm)"));
      beatIbi = 0;
    } else if (hrLinePending) {
      hrLinePending = false;
      printHrReport();
      resetTestStats();
    } else if (ms - lastB >= 1000) {
      lastB = ms;
      printGsrReport();
      hrLinePending = true;
    }
    return;
  }
  beatIbi = 0;
  if (ms - lastB >= 1000) {
    lastB = ms;
    printBpmLine();
  } else if (ms - lastDiag >= 10000) {
    lastDiag = ms;
    Serial.print(F("# late="));
    Serial.print(lateTicks);
    Serial.print(F(" resync="));
    Serial.print(resyncs);
    Serial.print(F(" hr="));                     // HR samples in the last ~10 s (expect ~1000)
    Serial.println(hrCount);
    hrCount = 0;
  }
}
