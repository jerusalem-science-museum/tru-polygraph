#include <Wire.h>
#include "MAX30105.h"
#include "heartRate.h"

MAX30105 particleSensor;

const byte RATE_SIZE = 6;       // beats averaged (smaller = faster, larger = smoother)
byte rates[RATE_SIZE];
byte rateSpot = 0;
byte validBeats = 0;            // how many good beats collected so far
long lastBeat = 0;
int beatAvg = 0;
unsigned long lastPrint = 0;

void setup() {
  Serial.begin(115200);

  if (!particleSensor.begin(Wire, I2C_SPEED_FAST)) {
    Serial.println("MAX30102 not found. Check wiring.");
    while (1);
  }

  // brightness, sampleAverage, ledMode, sampleRate, pulseWidth, adcRange
  particleSensor.setup(80, 4, 2, 400, 411, 4096);
  particleSensor.setPulseAmplitudeRed(0x0A);
}

void loop() {
  long irValue = particleSensor.getIR();

  if (irValue < 50000) {                    // no finger: reset everything
    validBeats = 0;
    rateSpot = 0;
    beatAvg = 0;
    lastBeat = 0;
    if (millis() - lastPrint > 1000) {
      Serial.println("No finger detected");
      lastPrint = millis();
    }
    return;
  }

  if (checkForBeat(irValue)) {
    long now = millis();
    long delta = now - lastBeat;
    lastBeat = now;

    if (delta > 0 && delta < 2000) {        // ignore the very first beat
      float bpm = 60000.0 / delta;

      // 1) must be in a sensible range (40-150 BPM)
      bool ok = (bpm >= 40 && bpm <= 150);

      // 2) once we have an average, reject jumps bigger than 25%
      if (ok && validBeats >= 2) {
        if (abs(bpm - beatAvg) > beatAvg * 0.25) ok = false;
      }

      if (ok) {
        rates[rateSpot++] = (byte)bpm;
        rateSpot %= RATE_SIZE;
        if (validBeats < RATE_SIZE) validBeats++;

        int sum = 0;
        for (byte i = 0; i < validBeats; i++) sum += rates[i];
        beatAvg = sum / validBeats;
      }
    }
  }

  // print once per second, only when we have a real reading
  if (millis() - lastPrint > 1000) {
    lastPrint = millis();
    if (validBeats >= 3) {
      Serial.print("Avg BPM=");
      Serial.println(beatAvg);
    } else {
      Serial.println("Measuring...");
    }
  }
}