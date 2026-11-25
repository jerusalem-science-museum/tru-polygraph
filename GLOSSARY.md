# Polygraph examination

A science-museum exhibit where an operator asks a visitor (the subject) a fixed list of questions while skin conductance and heart rate are recorded.

## Language

**Examination**:
One subject's full session: a rest period, the questions, and the truth classification afterwards.
_Avoid_: Test, session, measurement

**Operator**:
The museum staff member who asks the questions and drives the app.
_Avoid_: Questioner, examiner, user

**Subject**:
The visitor being examined.
_Avoid_: Participant, patient, user

**Question start mark**:
The moment the operator begins asking a question, recorded by the first Space press of that question.
_Avoid_: Question mark, question time

**Question end mark**:
The moment the operator finishes asking a question, recorded by the second Space press of that question.
_Avoid_: Question mark, question time

**Answer mark**:
The moment the operator records the subject's answer, by pressing one of the answer keys (1, 2, 3, ...).
_Avoid_: Answer time, response

**Truth classification**:
The operator's record, made after the questions, of whether each answer was actually true or a lie.
_Avoid_: Answer type

**Detection**:
The polygraph's own conclusion about whether an answer was true or a lie, to be compared with the truth classification.
_Avoid_: Result, verdict, prediction

**Lock**:
The state in which the heart-rate reading is trusted: enough consecutive beats agreed with each other. Outside a lock no BPM is reported.
_Avoid_: Valid reading, acquired

**Sensor outage**:
A stretch of the examination during which the heart-rate sensor does not answer; skin conductance keeps recording.
_Avoid_: Disconnect, no sensor

**Connection loss**:
A stretch during which the link to the Arduino is down; the examination is paused until it is restored.
_Avoid_: Disconnect, outage, serial error

**Question gap**:
The time since the last answer mark; the operator waits for the subject's skin conductance to recover before the next question start mark.
_Avoid_: Timer, gap timer
