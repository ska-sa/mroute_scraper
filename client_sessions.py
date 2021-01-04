""" mroute_client_session.py

This class extends asyncssh.SSHClientSession to handle the response to a "show ip mroute" query to a Mellanox switch. 

"""
import asyncssh
import sqlite3
import re
import sys
import traceback

from state_machines import IPMrouteParser

class ClientSessionBase(asyncssh.SSHClientSession):

    def __init__(self, db):
        """Initialise the session.

        We need an additional member variable of a string buffer over and above the base class.
        """
        super().__init__()  # Not sure how much this is necessary... but for completeness it should be there methinks.
        self.buffer: str = ""
        self.db = db # sqlite3.connect(":memory:")

    def __call__(self):
        return self

    def process_line(self, line: str):
        """Process the line held in the buffer.

        The specifics of this function need to be implemented in the derived class."""
        raise NotImplementedError
    
    def data_received(self, data, datatype):
        """Process data received via the SSH session.

        This function pushes incoming data into a buffer, until it detects a newline character. Once the newline is
        detected, we trigger some processing to happen on the line that is in the buffer, and reset it to receive the
        next incoming line.
        """
        #print(repr(data))
        if "\x1b" in data:
            # It's some sort of vty100 control sequence, we aren't interested.
            return
        while "\r\n" in data:
            self.buffer += data[:data.find("\r\n")]
            self.process_line(self.buffer)
            self.buffer = ""
            data = data[data.find("\r\n")+2:]

        if ">" in data:
            self.buffer = ""
        else:
            self.buffer += data

    def connection_lost(self, exc):
        if exc:
            print(f'SSH session error: ({type(exc)}) {str(exc)}', file=sys.stderr)
            print()


class IPMrouteClientSession(ClientSessionBase):
    
    def __init__(self, db):
        """Set up the appropriate table in the db."""
        super().__init__(db)
        # We need a state machine to parse the output of `show ip mroute`:
        self.mroute_parser_state: IPMrouteParser = IPMrouteParser.DEFAULT

        # scratchpad vars for the state machine to use
        self.mcast_group = ""

        # Set up the appropriate table in the database:
        #print(self.db)
        with self.db:
            cur = self.db.cursor()
            cur.execute("""CREATE TABLE subscriptions (
                ID INTEGER PRIMARY KEY,
                mcast_group text,
                port text
            );
            """)

    def process_line(self, line):
        """Process the line held in the buffer.

        This particular state machine just understands mroutes and stores them in a database, along with which ports want them.
        """

        # This re matches something that looks like a multicast group: (*, 239.10.10.20/32) for instance.
        multicast_group_re = re.compile(r"\(\*, \d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}/\d{1,2}\)")
        # This re matches anything that looks like an IP address preceded by the letters RP (for Rendezvous Point).
        rp_re = re.compile(r"RP \d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}")

        if self.mroute_parser_state == IPMrouteParser.DEFAULT:
            if "IP Multicast Routing Table" in line:
                # If we see this line, we are about to get our list of known routes.
                self.mroute_parser_state = IPMrouteParser.IGNORE_PREAMBLE
                self.mcast_group = ""
        elif self.mroute_parser_state == IPMrouteParser.IGNORE_PREAMBLE:
            if len(line) == 0:
                # We ignore the preamble up to the first blank line.
                self.mroute_parser_state = IPMrouteParser.CHECK_MCAST_GROUP   
        elif self.mroute_parser_state == IPMrouteParser.CHECK_MCAST_GROUP:
            if multicast_group_re.search(line) is not None:
                # We parse the line to get the mcast group and rp
                self.mcast_group = multicast_group_re.findall(line)[0]
                _rp = rp_re.findall(line)[0]
                self.mroute_parser_state = IPMrouteParser.PARSE_BIDIR_UPSTREAM
            else:
                # If the re doesn't match, then we are finished with the output of the command and on to something else.
                self.mroute_parser_state = IPMrouteParser.DEFAULT
        elif self.mroute_parser_state == IPMrouteParser.PARSE_BIDIR_UPSTREAM:
            # This line gives us the bidir upstream:
            _bidir_upstream = line.split(': ')[-1]  # We ignore it for now
            self.mroute_parser_state = IPMrouteParser.IGNORE_LIST_HEADING
        elif self.mroute_parser_state == IPMrouteParser.IGNORE_LIST_HEADING:
            # This line gives us the heading of the outgoing interface list. Ignore, no useful info here.
            self.mroute_parser_state = IPMrouteParser.PARSE_OUTGOING_INTERFACE_LIST
        elif self.mroute_parser_state == IPMrouteParser.PARSE_OUTGOING_INTERFACE_LIST:
            if len(line) > 0 and "\r" not in line:
                # Now we start to see which ports are interested.
                port = line.split(',')[0]  # TODO: should probably introduce logic to ignore the loopback interfaces. Or should I?
                #print(self.db)
                with self.db:
                    cur = self.db.cursor()
                    cur.execute("""INSERT INTO subscriptions (mcast_group, port) VALUES (?, ?)""", (self.mcast_group, port.strip()))
            elif "\r" in line:
                # We are done. \r only happens without \n if there's a prompt.
                self.mroute_parser_state = IPMrouteParser.DEFAULT
            else:
                # No more interested interfaces, start to look for the next group.
                self.mroute_parser_state = IPMrouteParser.CHECK_MCAST_GROUP
        else:
            # Something has gone wrong. 
            print("Something has gone quite wrong.")



class VersionClientSession(ClientSessionBase):
    """This actually should be removed. It's just reminding me to handle the lldp remotes at this point."""
    def data_received(self, data, datatype):
        print(data, end="")


class LLDPRemoteSession(ClientSessionBase):

    def __init__(self, db):
        super().__init__(db)

        # Scratchpad variables:
        self.port_name = ""
        self.requested_port = 0

        with self.db:
            cur = self.db.cursor()
            cur.execute("""CREATE TABLE lldp_remotes (
                ID INTEGER PRIMARY KEY,
                port_name text,
                remote_host text
            );
            """)


    def process_line(self, line):
        """Process the line held in the buffer.

        This is a fairly simple one, we ignore most of the output of the command, we are just looking for the remote host.
        No state machine needed as such.
        """
        port_name_re = re.compile(r"show lldp interfaces ethernet 1/\d{1,2}")  # We can't just use `if Eth in line` here because if we are connecting to a switch, it gets confused.
        remote_host = None
        
        if port_name_re.search(line) is not None:
            self.port_name = f"Eth1/{port_name_re.findall(line)[0][32:]}" 
            #print(f"Processing remote host on port {self.port_name}")
        #elif "No lldp remote information." in line:  # Can remove the comments if we explicitly need the non-blank ones. We might not.
        #    remote_host = "None"
        elif "Remote system name" in line:
            remote_host = line[20:].strip()
            
        if remote_host is not None:
            with self.db:
                cur = self.db.cursor()
                cur.execute("""INSERT INTO lldp_remotes (port_name, remote_host) VALUES (?,?)""",  (self.port_name, remote_host))
            remote_host = None
