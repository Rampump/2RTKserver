# 2RTK_NTRIPserver_Armbian_S805_OneCloud_EN_v1.0
This `2RTK_NTRIPserver_Armbian_S805_OneCloud_EN_v1.0.tar.gz` archive contains the `2rtk` executable program, packaged from Python code, and a `config.ini` configuration file, designed specifically for the Armbian operating system, such as when running on devices like the OneCloud WanKe Cloud.

The `2rtk` program's core function is to handle RTCM data from RTK modules. When in **serial mode** (set by `MODE = serial` in the `[General]` section of `config.ini`), it reads RTCM data from the USB serial port connected to the RTK module on the system and then sends this data to a self - hosted caster server or external ones like RTK2GO, based on the configurations in the `[NTRIP]` section. 

In **relay mode** (`MODE = relay`), the program acts as an NTRIP client. It fetches data from another specified NTRIP caster server configured in the `[Relay]` section and uploads it to your self - hosted caster server.

### Configuration Details
- **`[General]` Section**:
    - `MODE`: Determines the operating mode. Options are `serial` for serial port data handling and `relay` for data relaying from other casters.
- **`[Serial]` Section**: 
    - Applies only when `MODE = serial`.
    - `PORT`: Specifies the serial port device path. If left empty, the program automatically scans all available serial ports.
    - `BAUD_RATE`: Sets the baud rate. If not configured, it will automatically test common rates (initially starting with 115200, then 9600, 57600, and 230400). It's advisable to set the RTK module's baud rate to 115200 when using USB with Armbian.
- **`[NTRIP]` Section**:
    - Used to configure parameters for the local caster server.
    - `HOST`: The address or IP of the local caster server.
    - `PORT`: The port of the local caster (default is 2101).
    - `MOUNTPOINT`: The name of the caster mount point.
    - `PASSWORD`: The connection password for the caster. Leave blank if not applicable.
- **`[Relay]` Section**: 
    - Applicable when `MODE = relay`.
    - `HOST`: Address or IP of the relay caster server (e.g., `rtk2go.com`).
    - `PORT`: Port of the relay caster (default 2101).
    - `MOUNTPOINT`: Mount point name on the relay caster.
    - `USER` and `PASSWORD`: Credentials for the relay caster if Basic authentication is required.

With this package, users can easily deploy and configure the `2rtk` program on Armbian - based devices to manage RTCM data transfer between RTK modules, external caster servers, and their own self - hosted casters. 
