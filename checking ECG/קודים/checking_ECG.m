% Settings
port = "COM5";                  % Change if Arduino uses another port
baudRate = 115200;
measurementDuration = 60;       % Measurement duration in seconds

fileName = "ecg_pulse_withoutE.csv";

% Open serial communication with Arduino
arduino = serialport(port, baudRate);

configureTerminator(arduino, "LF");
arduino.Timeout = 2;

% Remove old data stored in the serial buffer
flush(arduino);

% Wait for the serial connection to stabilize
pause(2);

% Initialize arrays for storing the measurements
time = [];
voltage = [];
electrodesConnected = [];

disp("Starting ECG measurement...");

startTime = tic;

% Read data for the selected measurement duration
while toc(startTime) < measurementDuration

    % Check whether Arduino sent new data
    if arduino.NumBytesAvailable > 0

        receivedLine = strtrim(readline(arduino));
        currentTime = toc(startTime);

        % Check whether one of the electrodes is disconnected
        if receivedLine == "leads off!"

            time(end + 1, 1) = currentTime;
            voltage(end + 1, 1) = NaN;
            electrodesConnected(end + 1, 1) = false;

        else
            % Convert the received text into a numerical value
            measuredVoltage = str2double(receivedLine);

            % Store the measurement only if the conversion succeeded
            if ~isnan(measuredVoltage)

                time(end + 1, 1) = currentTime;
                voltage(end + 1, 1) = measuredVoltage;
                electrodesConnected(end + 1, 1) = true;

            end
        end
    end
end

disp("Measurement finished.");

% Create a table containing all measurements
ecgData = table( ...
    time, ...
    voltage, ...
    electrodesConnected, ...
    'VariableNames', ...
    {'Time_seconds', 'Voltage_V', 'Electrodes_connected'});

% Save the table as a CSV file
writetable(ecgData, fileName);

disp("Data saved to:");
disp(fullfile(pwd, fileName));


% Close the serial connection
clear arduino;
%% Plot all ECG data sets
% ECG measurement files
files_ecg = {
    "ecg_chest.csv"
    "ecg_palms.csv"
    "ecg_palms_P.csv"
    "ecg_fingers.csv"
    "ecg_fingers_P.csv"
    "ecg_pulse.csv"
    "ecg_pulse_P.csv"
    "ecg_pulse_withoutE"
    "ecg_fingers_different_RL"
};

% Create one figure with a 3-by-3 tiled layout
figure;

layout = tiledlayout(3, 3, ...
    'TileSpacing', 'compact', ...
    'Padding', 'compact');

title(layout, "ECG Measurements");

for i = 1:length(files_ecg)

    % Read the current ECG file
    data = readtable(files_ecg{i});

    % Extract time and voltage columns
    time = data.Time_seconds;
    voltage = data.Voltage_V;

    % Move to the next tile
    nexttile;

    % Plot voltage as a function of time
    plot(time, voltage, ...
        'LineWidth', 1.2, ...
        'Color', [0 0.45 0.74]);

    grid on;

    xlabel('Time [s]');
    ylabel('Voltage [V]');

    % Extract and clean the filename for the title title
    [~, plotTitle, ~] = fileparts(files_ecg{i});

    plotTitle = regexprep(plotTitle, "^ecg_", "");
       plotTitle = strrep(plotTitle, "_", " ");

    title(plotTitle, 'Interpreter', 'none');
end
%% plotting one data set
% Load the ECG measurement file
fileName ="ecg_fingers_different_RL.csv";
data = readtable(fileName);

% Extract time and voltage columns
time = data.Time_seconds;
voltage = data.Voltage_V;

% Plot voltage as a function of time
figure;

plot(time, voltage, ...
    'LineWidth', 1.2, ...
    'Color', [0 0.45 0.74]);

grid on;

xlabel('Time [s]');
ylabel('Voltage [V]');

% Create the graph title from the filename
[~, plotTitle, ~] = fileparts(fileName);

% Replace underscores with spaces
plotTitle = strrep(plotTitle, "_", " ");

title(plotTitle);

xlim([0 60]);
grid on;
title(plotTitle);

xlim([0 max(time)]);

figure;
% Calculate sampling frequency
Fs = 1 / mean(diff(time));

% Remove mean voltage
signal = voltage - mean(voltage);

% Calculate FFT
N = length(signal);
Y = fft(signal);

% Positive-frequency axis
frequency = (0:floor(N/2))' * Fs / N;

% Normalized one-sided amplitude
amplitude = abs(Y(1:floor(N/2)+1)) / N;
amplitude(2:end-1) = 2 * amplitude(2:end-1);

plot(frequency, amplitude, ...
        "LineWidth", 2);

xlabel("Frequency [Hz]");
ylabel("Amplitude [V]");
title("FFT of "+plotTitle)