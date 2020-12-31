"""state_machines.py

This file contains enums describing state machines for use in parsing the output from the Mellanox switches.
"""

from enum import Enum

class IPMrouteParser(Enum):
    DEFAULT = 1
    IGNORE_PREAMBLE = 2
    CHECK_MCAST_GROUP = 3
    PARSE_BIDIR_UPSTREAM = 4
    IGNORE_LIST_HEADING = 5
    PARSE_OUTGOING_INTERFACE_LIST = 6

