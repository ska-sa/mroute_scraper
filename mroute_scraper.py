#!/usr/bin/env python
import asyncio
import asyncssh
import sys
import re
import sqlite3
import socket # for inet_aton
import struct # for unpack

from optparse import OptionParser
from configparser import ConfigParser

from typing import List

from client_sessions import IPMrouteClientSession, LLDPRemoteSession, VersionClientSession


connection_options = {"term_type":"vt100", "term_size":(80,100)}


async def get_version(conn: asyncssh.SSHClientConnection):
    """Ask the switch what software version it is running."""
    chan, _session = await conn.create_session(VersionClientSession("foo"),  **connection_options)
    chan.write("show version\r\n")
    await asyncio.sleep(1)
    chan.write("exit\r\n")
    await chan.wait_closed()


async def scrape_lldp_remotes(conn: asyncssh.SSHClientConnection, db):
    chan, session = await conn.create_session(LLDPRemoteSession(db), term_type="vt100", term_size=(80,50))
    await asyncio.sleep(1)
    for n in range(1,37,1):
        chan.write(f"show lldp interfaces ethernet 1/{n} remote\n")
        await asyncio.sleep(0.2)
    chan.write("exit\n")
    await chan.wait_closed()
    if False:
        with session.db:
            cur = session.db.cursor()
            port_remotes = list(cur.execute("""SELECT port_name, remote_host FROM lldp_remotes;"""))
            for port_remote in port_remotes:
                print(port_remote)

async def scrape_mroutes(conn: asyncssh.SSHClientConnection, db):
    """Ask the switch which mroutes it knows, then parse the output and print it.
    
    I still need to decide whether this functionality really belongs in a separate class, perhaps in the client_sessions.py file.
    """
    chan, session = await conn.create_session(IPMrouteClientSession(db),  term_type="vt100", term_size=(80,10000000))  # We need a bajillion lines, to prevent the switch from doing a `more` kind of thing and getting the program stuck.
    chan.write("show ip mroute\r\n")
    await asyncio.sleep(1)
    chan.write("exit\r\n")
    await chan.wait_closed()
    if False:
        with session.db:
            cur = session.db.cursor()
            
            # I force it to make a list instead of an iterator / generator. Not sure if this is the most efficient way to do things.
            port_names = list(cur.execute("""SELECT DISTINCT port FROM subscriptions;"""))
            
            for port_name in port_names:
                #print(f"{port_name[0]}")
                group_count = cur.execute("""SELECT COUNT(mcast_group) FROM subscriptions WHERE port=?;""", (port_name[0],))
                for g in group_count:
                    print(f"Port {port_name[0]} wants {g[0]} mcast groups:")
                for group_name in cur.execute("""SELECT mcast_group FROM subscriptions WHERE port=?;""", (port_name[0],)):
                    print(f"\t\t{group_name[0]}")


async def run_client(host: str, configs: List[str]):
    """Run the ssh client.

    We create (currently) two concurrent sessions, one to scrape the mroutes, the other is basically a placeholder
    which prints out the version info of the switch.
    """

    central_db = sqlite3.connect(":memory:")

    if configs is not None:
        with central_db:
            cur = central_db.cursor()
            cur.execute("""CREATE TABLE maddr_names (
                maddr text PRIMARY KEY,
                name text
            );
            """)
            for filepath in configs:
                config = ConfigParser()
                config.read(filepath)
                # Behold my python kung-fu, and marvel...
                for mcast_ip, name in zip(config["fengine"]["source_mcast_ips"].split(","), config["fengine"]["source_names"].split(",")):
                    base_ip, ip_range, _port = re.split(r"[\+\:]", mcast_ip)
                    base_ip = struct.unpack("!L", socket.inet_aton(base_ip))
                    for i in range(int(ip_range) + 1):  # we need the plus one because the range is specified as <addr>+<n> in the config file, i.e. there are n+1 addresses. 
                        final_ip = socket.inet_ntoa(struct.pack("!L", base_ip[0] + i))
                        cur.execute("""INSERT OR IGNORE INTO maddr_names(maddr, name) VALUES (?, ?);""", (final_ip, f"{name}.{i}"))
    else:
        print("No config file(s) specified, we can still look at the mroutes but we can't say what they are for.")

    async with asyncssh.connect(host=host, username="monitor", password="monitor") as conn:
        # Creating the task first, then awaiting it, allows multiple tasks to run concurrently, as opposed to
        # completely finishing the first one, then only starting with the next.
        mroute_task = asyncio.create_task(scrape_mroutes(conn, central_db))
        lldp_remote_task = asyncio.create_task(scrape_lldp_remotes(conn, central_db))
        
        await lldp_remote_task
        await mroute_task

        with central_db:
            cur = central_db.cursor()
            
            # I force it to make a list instead of an iterator / generator. Not sure if this is the most efficient way to do things.
            port_names = list(cur.execute("""SELECT DISTINCT port FROM subscriptions;"""))
            
            for port_name in port_names:
                #print(f"{port_name[0]}")
                group_count = list(cur.execute("""SELECT COUNT(mcast_group) FROM subscriptions WHERE port=?;""", (port_name[0],)))
                remote_host = list(cur.execute("""SELECT remote_host FROM lldp_remotes WHERE port_name = ?;""", (port_name[0],)))
                if len(remote_host) < 1:
                    remote_host.append([])
                    remote_host[0].append("loopback")  # Just because the output of these sqlite3 things are weird.
                for g in group_count:
                    print(f"Port {port_name[0]} ({remote_host[0][0]}) wants {g[0]} mcast groups:")
                for group_name in cur.execute("""SELECT mcast_group FROM subscriptions WHERE port=?;""", (port_name[0],)):
                    print(f"\t\t{group_name[0]}")

                

if __name__ == "__main__":

    description = """This program connects to Mellanox switches via SSH and queries the mroutes known to the swith.
                     It then prints a summary of the multicast groups going out on each poirt.
                  """
    parser = OptionParser(description=description)
    parser.set_usage("%prog [options]")
    parser.add_option("-a", "--addr", type=str, default="10.8.96.57",
                      help="Hostname or ip address of Mlnx switch to contact.")
    parser.add_option("-c", "--config", action="append", type=str, default=None,
                      help="Path of config file to parse, to determine purposes of mcast traffic. Multiple files can be parsed by repeating the flag.")
    opts, _args = parser.parse_args()

    try:
        asyncio.get_event_loop().run_until_complete(run_client(host=opts.addr, configs=opts.config))
    except (OSError, asyncssh.Error) as exc:
        sys.exit('SSH connection failed: ' + str(exc)) 