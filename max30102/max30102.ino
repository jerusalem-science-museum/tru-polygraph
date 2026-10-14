#include <Wire.h>
#include "MAX30105.h"
#include "heartRate.h"
#include <math.h>

MAX30105 particleSensor;

// Sensor configuration
const byte INITIAL_LED_BRIGHTNESS = 60;
const byte IR_LED_BRIGHTNESS = 60; // IR emitter intensity
const byte RED_LED_BRIGHTNESS = 10; // led intensity, just to see the sensor is working.
const byte SAMPLE_AVERAGE = 4;
const byte LED_MODE = 2;          // Red + IR
const int SAMPLE_RATE = 400;
const int PULSE_WIDTH = 411;
const int ADC_RANGE = 16384;

// Measurement configuration
const uint32_t FINGER_THRESHOLD = 50000; // rel. units up to 18 bits.
const unsigned long NO_CONTACT_RESET_MS = 1000;
const unsigned long MIN_BEAT_INTERVAL_MS = 400;   // 150 BPM
const unsigned long MAX_BEAT_INTERVAL_MS = 1500;  // 40 BPM
const float MAX_BPM_CHANGE_FRACTION = 0.25;

// Rolling average of accepted beats
const byte RATE_SIZE = 4;
float rates[RATE_SIZE];
byte rateSpot = 0;
byte validBeats = 0;
byte consecutiveRejectedBeats = 0;

unsigned long lastDetectedBeatMs = 0;
unsigned long noContactStartMs = 0;
float lastAcceptedBpm = 0.0;
float averageBpm = 0.0;
bool measurementClearedForNoContact = false;

// beat_status values:
// 0 = no beat detected in this sample
// 1 = first beat detected; BPM cannot yet be calculated
// 2 = accepted beat
// 3 = rejected/suspicious beat

void resetMeasurement() {
  validBeats = 0;
  rateSpot = 0;
  consecutiveRejectedBeats = 0;
  lastDetectedBeatMs = 0;
  lastAcceptedBpm = 0.0;
  averageBpm = 0.0;
}

void addAcceptedBpm(float bpm) {
  rates[rateSpot] = bpm;
  rateSpot = (rateSpot + 1) % RATE_SIZE;

  if (validBeats < RATE_SIZE) {
    validBeats++;
  }

  float sum = 0.0;
  for (byte i = 0; i < validBeats; i++) {
    sum += rates[i];
  }
  averageBpm = sum / validBeats;
}

void printSample(unsigned long timeMs,
                 uint32_t irValue,
                 bool contactOk,
                 byte beatStatus,
                 float detectedBpm) {
  Serial.print(timeMs);
  Serial.print(',');
  Serial.print(irValue);
  Serial.print(',');
  Serial.print(contactOk ? 1 : 0);
  Serial.print(',');
  Serial.print(beatStatus);
  Serial.print(',');
  Serial.print(detectedBpm, 2);
  Serial.print(',');
  Serial.print(averageBpm, 2);
  Serial.print(',');
  Serial.println(consecutiveRejectedBeats);
}

void setup() {
  Serial.begin(115200);

  if (!particleSensor.begin(Wire, I2C_SPEED_FAST)) {
    Serial.println("ERROR: MAX30102 not found. Check wiring and power.");
    while (1) {
      delay(1000);
    }
  }

  particleSensor.setup(INITIAL_LED_BRIGHTNESS,
                       SAMPLE_AVERAGE,
                       LED_MODE,
                       SAMPLE_RATE,
                       PULSE_WIDTH,
                       ADC_RANGE);

  // Heart-rate detection uses the IR channel. The red LED is kept dim.
  particleSensor.setPulseAmplitudeIR(IR_LED_BRIGHTNESS);
  particleSensor.setPulseAmplitudeRed(RED_LED_BRIGHTNESS);

  Serial.println("time_ms,ir_value,contact_ok,beat_status,detected_bpm,average_bpm,consecutive_rejected");
}

void loop() {
  uint32_t irValue = particleSensor.getIR();
  unsigned long now = millis();
  bool contactOk = (irValue >= FINGER_THRESHOLD);
  byte beatStatus = 0;
  float detectedBpm = 0.0;
  bool resetAfterPrinting = false;

  if (!contactOk) {
    if (noContactStartMs == 0) {
      noContactStartMs = now;
    }

    if (!measurementClearedForNoContact &&
        now - noContactStartMs >= NO_CONTACT_RESET_MS) {
      resetMeasurement();
      measurementClearedForNoContact = true;
    }
  } else {
    noContactStartMs = 0;
    measurementClearedForNoContact = false;

    if (checkForBeat(irValue)) {
      if (lastDetectedBeatMs == 0) {
        lastDetectedBeatMs = now;
        beatStatus = 1;
      } else {
        unsigned long beatIntervalMs = now - lastDetectedBeatMs;
        lastDetectedBeatMs = now;

        if (beatIntervalMs >= MIN_BEAT_INTERVAL_MS &&
            beatIntervalMs <= MAX_BEAT_INTERVAL_MS) {
          detectedBpm = 60000.0 / beatIntervalMs;
          bool accepted = true;

          if (validBeats >= 2) {
            float allowedChange = lastAcceptedBpm * MAX_BPM_CHANGE_FRACTION;
            accepted = fabs(detectedBpm - lastAcceptedBpm) <= allowedChange;
          }

          if (accepted) {
            beatStatus = 2;
            consecutiveRejectedBeats = 0;
            lastAcceptedBpm = detectedBpm;
            addAcceptedBpm(detectedBpm);
          } else {
            beatStatus = 3;
            consecutiveRejectedBeats++;
          }
        } else {
          beatStatus = 3;
          consecutiveRejectedBeats++;
        }

        if (consecutiveRejectedBeats >= 3) {
          resetAfterPrinting = true;
        }
      }
    }
  }
  printSample(now, irValue, contactOk, beatStatus, detectedBpm);

  if (resetAfterPrinting) {
    resetMeasurement();
  }
}
