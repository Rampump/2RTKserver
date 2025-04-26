# Introduction to 2RTK NTRIP Server 1.8.6
2RTK NTRIP Server 1.8.6 is a NTRIP server program written in Python 3. It can run on systems such as Armbian and is mainly used for forwarding and relaying RTCM data. It is suitable for scenarios that require high-precision positioning, such as UAV surveying and mapping, precise positioning, and smart agriculture.

## Main Functions
- **Two Working Modes**:
    - **Serial Port Mode**: It can automatically scan USB serial ports. You can also specify a serial port to read RTCM data from the RTK module and then forward it to the local NTRIP Caster.
    - **Relay Mode**: As a NTRIP client, it connects to other NTRIP servers to obtain RTCM data and then forwards it to the local NTRIP Caster.
- **Automatic Retry**: When encountering network fluctuations, server connection failures, and other situations, the program will automatically reconnect and authenticate to ensure stable operation.
- **Easy Configuration**: You can set parameters such as serial ports, NTRIP server addresses, ports, mount points, and authentication information through the config.ini file, which is easy to modify.
- **Status View**: When the program is running, it will display information such as the current working mode, data sources and destinations, running time, and forwarded data volume on the console, making it convenient to monitor the operation.

## Deployment and Use
Currently, it is mostly used on the Armbian system of OneCloud. Just plug the RTK base station module into the USB port of OneCloud, and the program can read the data and forward it to the Caster. The deployment operation is simple. In addition, it can be adapted to RTK2GO.COM, providing more choices for data source and transmission.

## Technical Features
This program is based on single-base-station RTK technology. The base station observes satellite signals to calculate correction numbers, which are sent to the rover station to help the rover calculate its position accurately. Its advantages are simple structure and low cost. However, its effective range is limited, generally within 20 - 50 kilometers around the base station. As the distance increases, the error will gradually increase. Compared with multi-base-station RTK technologies such as Qianxun CORS and Mobile CORS, its coverage is smaller. But it can provide accurate positioning within a small range, making it suitable for users with a limited budget. 
