# Source Code Explanation of 2RTK NTRIP Server 1.8.6

## I. Project Overview
The 2RTK NTRIP Server 1.8.6 is an NTRIP (Networked Transport of RTCM via Internet Protocol) server program developed in Python 3. It is primarily designed to achieve the forwarding and relaying of RTCM (Radio Technical Commission for Maritime Services) data in system environments such as Armbian. It supports reading RTCM data from RTK modules via USB serial ports or pulling data from other NTRIP servers, and then forwarding it to the specified NTRIP Caster. This makes it suitable for applications related to high-precision positioning.

## II. Core Functions
1. **Dual-Mode Operation**
    - **Serial Port Mode**: Automatically scans for available USB serial ports or uses the specified one to read RTCM data from RTK modules and forward it to the local NTRIP Caster.
    - **Relay Mode**: Acts as an NTRIP client to connect to other NTRIP servers, obtain RTCM data streams, and re-broadcast them to the local NTRIP Caster.
2. **Automatic Retry Mechanism**: Key network connection and operation functions (such as server connection and handshake authentication) are equipped with an automatic retry feature, ensuring the program's stability in case of network fluctuations or other abnormal situations.
3. **Configuration File-Driven**: Manages operating parameters through the `config.ini` file, including serial port settings, NTRIP server addresses, ports, mount points, authentication information, etc., providing users with flexible configuration options.
4. **Real-Time Status Monitoring**: Regularly refreshes the program's running status on the console, displaying information such as the current mode, data source and destination, running duration, and forwarded data volume, enabling users to keep track of the operation in real-time.

## III. Code Structure and Key Modules
### (I) Decorators and Utility Functions
1. **`retry` Decorator**: Implements the automatic retry logic for functions. It allows specifying the maximum number of retries and the retry interval, used to handle exceptions such as network connection failures and operation timeouts.
2. **`format_bytes` Function**: Converts the number of bytes into a human-readable format (e.g., B, KB, MB, etc.), facilitating the display of data volume.
3. **`format_duration` Function**: Converts the total number of seconds into a format of hours, minutes, and seconds, used to display the program's running duration.

### (II) Basic Functional Functions
1. **`setup_logging` Function**: Configures the logging system to output running information to the console, which is convenient for debugging and monitoring.
2. **`get_config` Function**: Parses the `config.ini` configuration file and extracts operating parameters, returning them in dictionary form.
3. **`validate_config` Function**: Verifies the validity of configuration parameters, ensuring that key information is correctly configured to avoid runtime errors.

### (III) Serial Port-Related Functions
1. **`detect_serial` Function**: Automatically detects available USB serial ports according to the configuration or verifies whether the specified serial port is transmitting data, and returns the available serial port device and baud rate.
2. **`test_serial` Function**: Attempts to read data from the serial port at the specified baud rate to determine whether the serial port is working properly.

### (IV) NTRIP Connection Functions
1. **`connect_to_host` Function**: Establishes a TCP connection with the specified NTRIP server.
2. **`build_client_request` Function**: Constructs the NTRIP client request header, including authentication information (if any), for handshaking with the NTRIP server.
3. **`handshake_ntrip_client` Function**: Sends the request header and verifies the response from the NTRIP server to ensure successful authentication.
4. **`open_caster` Function**: Connects to the local NTRIP Caster, completes the authentication process, and obtains the data sending channel.

### (V) Data Transmission Functions
1. **`upload_via_serial` Function**: In serial port mode, continuously reads RTCM data from the USB serial port and sends it to the local NTRIP Caster. It includes timeout reconnection and status refresh logic.
2. **`forward_relay` Function**: In relay mode, receives data from the remote NTRIP server and forwards it to the local NTRIP Caster. It also has exception handling and status display capabilities.

### (VI) Reconnection Helper Functions
1. **`open_relay` Function**: In relay mode, establishes a connection with the remote NTRIP server and completes the authentication process.
2. **`reconnect_caster` Function**: When the connection with the local NTRIP Caster is interrupted, attempts to reconnect.

### (VII) Main Function
The **`main` function** is the entry point of the program. It is responsible for initializing the logging system, reading the configuration, validating parameters, starting the corresponding functions according to the mode, and catching exceptions during operation to implement the automatic retry mechanism.

## IV. Configuration File Description
The `config.ini` file contains the following key configuration items:
1. **`[General]`**
    - `MODE`: Operating mode, with options of `serial` (serial port mode) or `relay` (relay mode).
2. **`[Serial]`** (only valid in serial port mode)
    - `PORT`: USB serial port device path. If left blank, it will be automatically scanned.
    - `BAUD_RATE`: Serial port baud rate. If left blank, common baud rates (115200, 9600, 57600, 230400) will be automatically attempted.
3. **`[NTRIP]`**
    - `HOST`: Address or IP of the local NTRIP Caster server.
    - `PORT`: Port of the local NTRIP Caster, with a default value of 2101.
    - `MOUNTPOINT`: Mount point name.
    - `PASSWORD`: Connection password (if applicable).
4. **`[Relay]`** (only valid in relay mode)
    - `HOST`: Address or IP of the relay NTRIP server.
    - `PORT`: Port of the relay NTRIP server, with a default value of 2101.
    - `MOUNTPOINT`: Mount point name of the relay server.
    - `USER`: Authentication username (if applicable).
    - `PASSWORD`: Authentication password (if applicable).

## V. Operation and Deployment
1. **Environment Requirements**: Python 3 running environment. It is recommended to deploy on the Armbian system and install dependent libraries such as `pyserial` and `configparser`.
2. **Startup Method**: Execute `python3 2rtk.py` in the terminal, and the program will start corresponding functions according to the configuration in `config.ini`.

## VI. Contribution and Feedback
Developers are welcome to contribute code, submit `issue` to report problems, or propose functional optimization suggestions to jointly improve the 2RTK NTRIP Server project. 
