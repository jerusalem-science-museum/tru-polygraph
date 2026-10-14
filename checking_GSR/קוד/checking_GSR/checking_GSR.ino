const int gsrPin = A0;

const unsigned long sampleInterval = 2000;  // 2000 µs = 500 Hz

void setup() {
  Serial.begin(115200);
}

void loop() {
  static unsigned long previousMicros = 0;

  unsigned long now = micros();

  if (now - previousMicros >= sampleInterval) {
    previousMicros += sampleInterval;

    int raw = analogRead(gsrPin);

    Serial.print(now);
    Serial.print(",");
    Serial.println(raw);
  }
}