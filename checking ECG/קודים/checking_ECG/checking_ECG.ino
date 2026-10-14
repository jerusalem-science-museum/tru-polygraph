const int loPlusPin  = 8;
const int loMinusPin = 9;
const int ecgPin     = A0;

const float maxVoltage = 5;

void setup() {
  Serial.begin(115200);

  pinMode(loPlusPin, INPUT);
  pinMode(loMinusPin, INPUT);
}

void loop() {

  if ((digitalRead(loPlusPin) == HIGH) ||
      (digitalRead(loMinusPin) == HIGH)) {

    Serial.println("leads off!");
  }

  else {
    int rawValue = analogRead(ecgPin);

    float voltage = rawValue * maxVoltage / 1023.0;

    Serial.println(voltage, 3);
  }

  delay(1);
}
