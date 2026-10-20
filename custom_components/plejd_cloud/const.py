"""Constants for the Plejd integration.

Protocol facts here were reverse-engineered from our own analysis of the Plejd
Android app (.NET MAUI, assembly `Plejd.dll` / `Plejd.Shared`). See
docs/reverse_engineering.md for the full decode.
"""

from __future__ import annotations

DOMAIN = "plejd_cloud"

# Prefix for a room's pseudo-device identifier/unique_id (PlejdRoomLight). Rooms live in the
# same (DOMAIN, id) identifier namespace as real Plejd devices but have no Parse cloud object
# of their own — anything that mirrors HA device state back to the Plejd cloud by device_id
# must recognize and skip this prefix.
ROOM_DEVICE_ID_PREFIX = "room_"

# BLE address of the mesh device discovered during the config flow.
CONF_DISCOVERED_ADDRESS = "discovered_address"

# Config-entry data keys.
CONF_SITE_ID = "site_id"
CONF_CRYPTO_KEY = "crypto_key"  # hex string of the 16-byte site key
CONF_DEVICES = "devices"  # cached device list (so HA works offline after setup)
CONF_DEVICE_ADDRESSES = "device_addresses"  # device_id -> physical mesh address, for fault polling
CONF_SCENES = "scenes"  # cached scene list
CONF_ROOMS = "rooms"  # cached room list (name + group mesh address + member output addresses)
# Every room's title by room_id, including rooms CONF_ROOMS leaves out (no safe light-only group).
CONF_ROOM_NAMES = "room_names"
CONF_INPUTS = "inputs"  # cached button-input list
CONF_MOTION = "motion"  # cached motion-sensor list
CONF_PENDING_ROOM_MOVES = "pending_room_moves"  # move_device_to_room's own not-yet-cloud-confirmed moves
HARDWARE_WMS_01 = 70  # motion sensor
HARDWARE_GWY_01 = 4  # gateway
CONF_GATEWAYS = "gateways"  # gateway (GWY-01) device ids; remote control is available when non-empty
CONF_RESOURCE_SET_ID = "resource_set_id"  # Resource-Set-ID for the remote-control WebSocket
CONF_INSTALLATION_ID = "installation_id"  # stable client GUID (Client-ID header)

# Transport preference (entry option) — force a comms interface, or auto-select.
CONF_TRANSPORT = "transport"
TRANSPORT_AUTO = "auto"  # gateway first when present, else Bluetooth (with fallback)
TRANSPORT_GATEWAY = "gateway"  # remote/cloud gateway only
TRANSPORT_BLE = "ble"  # local Bluetooth only

# ── BLE GATT layout (confirmed from Plejd.Shared PlejdConstants.BleCharacteristics) ──
# All characteristics live under the service base UUID with the 3rd 16-bit group
# (the "0001") swapped for the role suffix below.
PLEJD_SERVICE_UUID = "31ba0001-6085-4726-be45-040c957391b5"

# Bluetooth SIG company identifier Plejd uses in advertisement manufacturer data
# (Plejd.Shared PlejdManufacturerId; confirmed against a live capture).
PLEJD_BLE_COMPANY_ID = 887


def _char(suffix: str) -> str:
    return PLEJD_SERVICE_UUID.replace("0001", suffix, 1)


PLEJD_CHAR_NODE_INDEX_UUID = _char("0002")  # NodeIndex
PLEJD_CHAR_NODE_INDEX_DATA_UUID = _char("0003")  # NodeIndexData
PLEJD_CHAR_DATA_UUID = _char("0004")  # Datavector — write mesh commands here (encrypted)
PLEJD_CHAR_LAST_DATA_UUID = _char("0005")  # LastChangedDatavector — state notifications (encrypted)
PLEJD_CHAR_ACCESS_ADDRESS_UUID = _char("0006")  # AccessAddress
PLEJD_CHAR_DFU_UUID = _char("0007")  # DeviceFirmwareUpdate
PLEJD_CHAR_CRYPTO_KEY_UUID = _char("0008")  # CryptoKey (Diffie-Hellman exchange)
PLEJD_CHAR_AUTH_UUID = _char("0009")  # AuthKey (challenge/response)
PLEJD_CHAR_PING_UUID = _char("000a")  # PingPong (verify login)
PLEJD_CHAR_SPECIAL_UUID = _char("000b")  # SpecialCommand
PLEJD_CHAR_PRODUCT_UUID = _char("000c")  # ProductSpecific

# Only Datavector (0004) and LastChangedDatavector (0005) carry encrypted payloads;
# the auth/ping/key-exchange characteristics are plaintext.
PLEJD_ENCRYPTED_CHARS = (PLEJD_CHAR_DATA_UUID, PLEJD_CHAR_LAST_DATA_UUID)

# Default crypto key used while a device is on the unconfigured "default mesh".
DEFAULT_MESH_CRYPTO_KEY = bytes.fromhex("00112233445566778899aabbccddeeff")

# Encryption applies only to firmware >= this build stamp; older devices are plaintext.
ENCRYPTION_MIN_FIRMWARE = 20160801163820

# ── Mesh command opcodes (2-byte big-endian, from the app's mesh command schema) ──
# The command sits at offset 3 of the Datavector payload (after a 5-byte header).
CMD_OUTPUT_STATE_AND_LEVEL = 0x00C8  # set/report on-off + dim level (output, state, level[2])
CMD_OUTPUT_MIN_LEVEL = 0x00C9
CMD_OUTPUT_MAX_LEVEL = 0x00CA
CMD_OUTPUT_SPEED = 0x00CB  # set/report dim transition time (output, u16le steps + >0.5s flag)
CMD_OUTPUT_CURVE_TYPE = 0x00CC  # set/report dimming curve (output, LoadCurve byte)
CMD_OUTPUT_PHASE_DIM_TYPE = 0x00CE  # set/report phase-dim edge (output, PhaseOutputType byte)
CMD_OUTPUT_RELAY_OFF_TIME = 0x00D4  # minimum relay off time (output, u16le centiseconds)
CMD_OUTPUT_BOOT_STATE = 0x00D7  # after-power-outage state (output[, 0x00=off]; 1-byte=use_last)
CMD_OUTPUT_INRUSH_CURRENT = 0x00A2  # inrush current protection time (output, u16le centiseconds; 0=disabled)
CMD_OUTPUT_RELAY_CONFIG = 0x022A  # relay pole configuration (output, RelayConfig wire byte)
CMD_OUTPUT_START_LEVEL = 0x00CF  # set/report dim start level (output, u16le of fraction*65535)

# Dimming-curve options (LoadCurve enum, app: Standard/Linjär/Logaritmisk/Partiell).
CURVE_OPTIONS: dict[str, int] = {
    "standard": 0,
    "linear": 1,
    "logarithmic": 2,
    "partial": 3,
}
# Light-source / phase-dim type (PhaseOutputType enum: TrailingEdge=0, LeadingEdge=1).
# "Halogen" and "Non-dimmable LED" in the Plejd app are predefined cloud loads that
# configure LoadCurve + Phase together — they are NOT PhaseOutputType values (2 and 3
# map to PhaseOutputType.Invalid and are silently discarded by the firmware).
PHASE_DIM_OPTIONS: dict[str, int] = {
    "trailing_edge": 0,
    "leading_edge": 1,
}
# Hardware that actually phase-cuts (app's IPhaseable: DIM-01/02 family, SPD-01, FAK-01).
# Constant-current LED drivers, DALI, and downlights dim but aren't phase dimmers.
PHASE_DIM_HARDWARE: frozenset[int] = frozenset({1, 2, 11, 14, 15, 22, 24, 25, 40, 164})
# After-power-outage boot state options (0x00D7); True=restore last state, False=boot off.
BOOT_STATE_OPTIONS: dict[str, bool] = {
    "previous_state": True,
    "off": False,
}
# Hardware with a real relay that supports minimum relay-off time (IRelayOffTimeable, 0x00D4).
RELAY_HARDWARE: frozenset[int] = frozenset({3, 8, 17, 18})
# Wire bytes for relay pole config (app GetBytes: TwoPole→0, OnePole→1; enum values differ).
RELAY_POLE_OPTIONS: dict[str, int] = {"two_pole": 0, "one_pole": 1}
# Hardware with relay pole config (IRelayConfigurable: DIM-01-2P hw 11, DIM-01-2P-LC2 hw 25).
RELAY_CONFIG_HARDWARE: frozenset[int] = frozenset({11, 25})
CMD_SCENE = 0x0021  # execute scene
CMD_TIME_EVENT_TIME = 0x0258  # set/remove a time event's schedule
CMD_TIME_EVENT_TYPE = 0x0259  # what a time event does (TimeEventResult)
CMD_TIME_EVENT_SCENE = 0x025A  # the scene a time event runs

# Time events (on-device weekly schedules). 20 slots per device; weekday bit = 1<<index
# with Monday=0..Sunday=6 (app's Weekday enum); recurring events repeat "forever".
TIME_EVENT_SLOTS = 20
TIME_EVENT_RESULT_SCENE = 0
TIME_EVENT_REP_FOREVER = 0xFFFFFFFF
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
CONF_SCHEDULES = "schedules"  # entry.options: list of time-event schedule dicts
CONF_SHOW_PANEL = "show_panel"  # entry.options: show the Plejd dashboard in the sidebar (default True)
CONF_SYNC_AREAS = "sync_areas"  # entry.options: move devices to the HA area of their Plejd room (default False)

# Plejd-cloud schedules (the app's "Schemaläggning": astro-relative sunset/sunrise triggers
# plus an optional night-reduction quiet-hours window) — a separate, cloud-only feature from
# the on-device weekly time-events above (CONF_SCHEDULES, CMD_TIME_EVENT_*): confirmed via a
# live capture to never touch the BLE mesh, instead updating one TimeEvent Parse object plus
# one or two ordinary (hidden) scenes it runs. Only "astro" mode was captured; a fixed-clock
# trigger mode's wire shape is unconfirmed and not implemented.
CONF_CLOUD_SCHEDULES = "cloud_schedules"  # entry.data: schedules created via this integration
SCHEDULE_ASTRO_EVENTS = ("sunset", "sunrise")
SCHEDULE_OFFSET_MIN = -120
SCHEDULE_OFFSET_MAX = 120

# Holiday mode (presence simulation, the Plejd app's "Semesterläge") — entry.options.
CONF_HOLIDAY_LIGHTS = "holiday_lights"  # target light entity_ids; empty/absent = all Plejd lights
CONF_HOLIDAY_WINDOW_START = "holiday_window_start"  # "HH:MM", start of the active window
CONF_HOLIDAY_WINDOW_END = "holiday_window_end"  # "HH:MM", end of the window (may be < start = crosses midnight)
HOLIDAY_WINDOW_START_DEFAULT = "18:00"
HOLIDAY_WINDOW_END_DEFAULT = "23:00"
CMD_INPUT_STATE_AND_LEVEL = 0x0195  # state notification: channel, state, level[2]
CMD_SYSTEM_TIME = 0x001B
CMD_DEVICE_TYPE = 0x0000
CMD_DEVICE_MAC = 0x0003
CMD_DEVICE_FW_VERSION = 0x0004
CMD_GROUP_STATE_AND_LEVEL = 0x0098  # broadcast on/off + level to a group/all
CMD_INPUT_BUTTON = 0x0097  # button press broadcast on an input address: data=[01 pressed/00 released]
CMD_OUTPUT_SET = 0x0420  # modern "mini-package" output protocol (newer firmware)
SUBPKG_SOURCE = 3  # mini-package sub-package type: source flag
SUBPKG_LUX = 6  # mini-package sub-package type: ambient light
SUBPKG_WINDOW = 7  # WindowControl sub-package
SOURCE_APP = 8  # SourceFlags.App
WINDOW_STOP = 0  # WindowControlType.Stop
WINDOW_LEVEL = 1  # WindowControlType.Level
SOURCE_MOTION = 3  # SourceFlags.Motion
CMD_NOTIFY_EVENTS = 0x002B  # device fault flags (NotifyEvents bitfield)
CMD_HARDFAULT_REASON = 0x001D  # struct: code(u32 le), line(u16 le), message(ascii)
CMD_TRM_SETPOINT = 0x045C  # thermostat target temperature: u16le = round(C*10)
CMD_TRM_MODE = 0x045F  # thermostat operating mode (OperatingMode enum)
CMD_TRM_TEMP_READING = 0x045B  # temperature reading [sensor] -> u16le C*10
# Join/leave a room's mesh group (confirmed via a live capture of the app's own "move
# device to room" action - the device's own address, not its output address, is the
# command's target). data=[0x01, room_group_address] to leave, +[0x01] appended to join.
CMD_MESH_GROUP_MEMBERSHIP = 0x0008

# Thermostat operating modes (Plejd OperatingMode enum) -> HA presets.
TRM_MODE_SERVICE = 0  # thermostat off (shown as HVACMode.OFF, not a preset)
TRM_MODE_VACATION = 2
TRM_MODE_BOOST = 3
TRM_MODE_FROST_PROTECTION = 4
TRM_MODE_NIGHT_REDUCTION = 5
TRM_MODE_DAY_REDUCTION = 6
TRM_MODE_NORMAL = 7
# HA's standard preset names (home/away/sleep/eco get translated labels and icons).
TRM_PRESETS = {
    TRM_MODE_NORMAL: "home",
    TRM_MODE_VACATION: "away",
    TRM_MODE_BOOST: "boost",
    TRM_MODE_FROST_PROTECTION: "frost",
    TRM_MODE_NIGHT_REDUCTION: "sleep",
    TRM_MODE_DAY_REDUCTION: "eco",
}

# ── Cloud (Parse Server) — production constants, app-level (not user secrets) ──
PLEJD_PARSE_URL = "https://cloud.plejd.com/parse/"
PLEJD_PARSE_APP_ID = "zHtVqXt8k4yFyk2QGmgp48D9xZr2G94xWYnF4dak"
PLEJD_PARSE_LOGIN = "login"  # POST {username, password} -> {sessionToken}
PLEJD_FN_SITE_LIST = "functions/getSiteList"  # -> [{siteId, ...}]
PLEJD_FN_SITE_BY_ID = "functions/getSiteById"  # {siteId} -> site w/ cryptoKey + devices
PLEJD_FN_FIRMWARE_BY_HW = "functions/getFirmwaresByHardwareId"  # {hardwareId, faceplateId} -> [] when up to date
PLEJD_FN_UPDATE_DEVICE = (
    "functions/updateDevice_V2"  # {siteId, deviceId, deviceParseId, title|traits|hiddenFromRoomList}
)
PLEJD_FN_CREATE_DEVICE = "functions/createPlejdDevice_V2"  # register new device to site
PLEJD_FN_COMPATIBLE_DEVICES = "functions/getCompatibleDevices"  # {devices:[{hardwareId,...}]} -> neededAddresses
PLEJD_FN_CREATE_ROOM = "functions/createRoom"  # {siteId, roomId, title, category, imageHash} -> int
PLEJD_FN_UPDATE_ROOM = "functions/updateRoom"  # {siteId, roomId, title?, order?, category?, imageHash?} -> bool
PLEJD_FN_REMOVE_ROOM = "functions/removeRoom"  # {siteId, roomId} -> bool; cloud requires the room to be empty
PLEJD_FN_SET_INPUT = "functions/setPlejdDeviceInputSetting"  # {siteId, deviceId, input, buttonType, ...} -> bool
PLEJD_FN_CREATE_SCENE = (
    "functions/createScene"  # {siteId, sceneId, title, order, sceneSteps, hiddenFromSceneList, settings} -> int
)
PLEJD_FN_UPDATE_SCENE = (
    "functions/updateScene"  # {siteId, sceneId, title?|order?|sceneSteps?|hiddenFromSceneList?|settings?} -> bool
)
PLEJD_FN_REMOVE_SCENE = "functions/removeScene"  # {siteId, sceneId} -> bool
PLEJD_FN_REMOVE_DEVICE = "functions/removeDevice"  # {siteId, deviceId} -> bool
PLEJD_FN_CREATE_TIME_EVENT = (
    "functions/createTimeEvent_V3"  # {siteId, timeEventId: null, sceneId, scheduledDays, fadeTime,
    # activated, dirtyDevices, dirtyRemovedDevices: [], dirtyRemove: false, mode, version, start,
    # end, pauseStart?, pauseEnd?, nightReduction?, targetDevices} -> {targetDevices, eventId};
    # timeEventId is server-generated - the response's eventId is the real id, confirmed distinct
    # from updateTimeEvent_V3 (this integration wrongly used update for create until this was
    # captured)
)
PLEJD_FN_UPDATE_TIME_EVENT = (
    "functions/updateTimeEvent_V3"  # {siteId, timeEventId, sceneId, scheduledDays, fadeTime,
    # activated, dirtyDevices, dirtyRemovedDevices, dirtyRemove, mode, version, start, end,
    # pauseStart?, pauseEnd?, nightReduction?} -> {targetDevices, eventId}; whole-state call,
    # resent in full each edit
)
PLEJD_FN_REMOVE_TIME_EVENT = "functions/removeTimeEvent_V3"  # {siteId, timeEventId, mode, deviceIds} -> bool

# Room.RoomCategory enum values, sent to createRoom/updateRoom's "category" field as-is
# (the app does `.ToString()` on the enum). Excludes KidsRoom - the app itself marks it
# obsolete ("old KidsRoom rooms now use Bedroom instead") and hides it from the picker.
ROOM_CATEGORIES = (
    "LivingRoom",
    "Kitchen",
    "Outdoor",
    "Bedroom",
    "Hallway",
    "Bathroom",
    "Office",
    "Laundry",
    "Storage",
    "Other",
    "Garage",
)

# ── Device types (Plejd.Shared HardwareType enum: id -> product name) ──
HARDWARE_TYPES: dict[int, str] = {
    0: "Unknown",
    1: "DIM-01",
    2: "DIM-02",
    3: "CTR-01",
    4: "GWY-01",
    5: "LED-10",
    6: "WPH-01",
    7: "CCL-01",
    8: "SPR-01",
    9: "DWN-01-LC",
    10: "WRT-01",
    11: "DIM-01-2P",
    12: "DAL-01",
    13: "DEV-01",
    14: "DIM-01-LC",
    15: "DIM-02-LC",
    16: "JAL-01",
    17: "REL-01-2P",
    18: "REL-02",
    19: "EXT-01",
    22: "DIM-01-LC2",
    23: "LED-10-V2",
    24: "DIM-02-LC2",
    25: "DIM-01-2P-LC2",
    36: "LED-75",
    38: "WPH-01-LC",
    39: "PLF-02",
    40: "SPD-01",
    41: "DWN-02-LC",
    42: "WRT-01-LC",
    70: "WMS-01",
    71: "LPN-01",
    100: "TRM-01",
    102: "WIN-01",
    103: "OUT-01",
    105: "TRL-01",
    135: "OUT-02",
    164: "FAK-01",
    167: "DWN-01",
    196: "MTR-01",
    199: "DWN-02",
    228: "LED-75-V2",
}

# Per-output capability bitmask (Plejd.Shared DeviceTrait [Flags]).
TRAIT_POWERABLE = 0x01
TRAIT_DIMMABLE = 0x02
TRAIT_WHITE_TUNABLE = 0x04
TRAIT_GROUPABLE = 0x08
TRAIT_COVERABLE = 0x10
TRAIT_CLIMATE = 0x20
TRAIT_COVER_TILTABLE = 0x40

# Cloud per-output classification (DeviceInfo.OutputType) — the best HA-platform hint.
OUTPUT_TYPE_UNKNOWN = 0
OUTPUT_TYPE_LIGHT = 1
OUTPUT_TYPE_RELAY = 2
OUTPUT_TYPE_COVERABLE = 3
OUTPUT_TYPE_THERMOSTAT = 4

# Internal device categories (NOT HA platform names) → HA platform mapping:
#   light/switch/cover/climate map 1:1; motion/contact → binary_sensor;
#   button → event; none → device-only (no entity).
# This is the default by hardware type when cloud OutputType/traits are absent;
# refine with the per-output OutputType + Dimmable trait (see model/docs).
CATEGORY_LIGHT = "light"
CATEGORY_SWITCH = "switch"
CATEGORY_COVER = "cover"
CATEGORY_CLIMATE = "climate"
CATEGORY_MOTION = "motion"  # → binary_sensor (motion)
CATEGORY_CONTACT = "contact"  # → binary_sensor (opening)
CATEGORY_BUTTON = "button"  # → event
CATEGORY_NONE = "none"  # gateway/aux — no entity

DEFAULT_CATEGORY: dict[int, str] = {
    1: CATEGORY_LIGHT,
    2: CATEGORY_LIGHT,
    3: CATEGORY_LIGHT,
    5: CATEGORY_LIGHT,
    7: CATEGORY_LIGHT,
    9: CATEGORY_LIGHT,
    11: CATEGORY_LIGHT,
    12: CATEGORY_LIGHT,
    14: CATEGORY_LIGHT,
    15: CATEGORY_LIGHT,
    22: CATEGORY_LIGHT,
    23: CATEGORY_LIGHT,
    24: CATEGORY_LIGHT,
    25: CATEGORY_LIGHT,
    36: CATEGORY_LIGHT,
    39: CATEGORY_LIGHT,
    40: CATEGORY_LIGHT,
    41: CATEGORY_LIGHT,
    71: CATEGORY_LIGHT,
    103: CATEGORY_LIGHT,
    105: CATEGORY_LIGHT,
    135: CATEGORY_LIGHT,
    167: CATEGORY_LIGHT,
    199: CATEGORY_LIGHT,
    228: CATEGORY_LIGHT,
    8: CATEGORY_SWITCH,
    17: CATEGORY_SWITCH,
    18: CATEGORY_SWITCH,
    16: CATEGORY_COVER,
    196: CATEGORY_COVER,
    100: CATEGORY_CLIMATE,
    70: CATEGORY_MOTION,
    102: CATEGORY_CONTACT,
    6: CATEGORY_BUTTON,
    38: CATEGORY_BUTTON,
    10: CATEGORY_BUTTON,
    42: CATEGORY_BUTTON,
    4: CATEGORY_NONE,
    13: CATEGORY_NONE,
    19: CATEGORY_NONE,
    164: CATEGORY_NONE,
}

# Device fault flags reported by CMD_NOTIFY_EVENTS (NotifyEvents [Flags] u64).
NOTIFY_EVENT_FLAGS: dict[int, str] = {
    0x1: "hard_fault",
    0x2: "soft_overcurrent",
    0x4: "heavy_overcurrent",
    0x8: "overtemperature",
    0x10: "faceplate_detect_fail",
    0x20: "reset_watchdog",
    0x40: "reset_cpu_lock",
    0x80: "reset_pin",
    0x100: "reset_soft",
    0x200: "settings_driver",
    0x400: "low_power_wdt",
    0x800: "temperature_throttling",
    0x1000: "factory_reset_mesh_kept",
    0x2000: "boot_single_faulty_bank",
    0x4000: "overloaded",
    0x8000: "wrong_zcd",
    0x10000: "uart_error",
    0x20000: "dont_dim",
    0x40000: "adv_timeout",
    0x80000: "product_hw_fault_a",
    0x100000: "product_hw_fault_b",
    0x200000: "group_setting_fault",
}
