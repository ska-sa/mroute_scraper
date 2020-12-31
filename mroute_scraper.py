#!/usr/bin/env python
import asyncio
import asyncssh
import sys
import re
import sqlite3

from optparse import OptionParser

from mroute_client_session import IPMrouteClientSession

class VersionClientSession(asyncssh.SSHClientSession):
    """This actually should be removed. It's just reminding me to handle the lldp remotes at this point."""
    def data_received(self, data, datatype):
        print(data, end="")


async def get_version(conn: asyncssh.SSHClientConnection):
    """Ask the switch what software version it is running."""
    chan, _session = await conn.create_session(VersionClientSession, term_type="vt100", term_size=(80,100000))
    chan.write("show version\r\n")
    await asyncio.sleep(1)
    chan.write("exit\r\n")
    await chan.wait_closed()


async def scrape_mroutes(conn: asyncssh.SSHClientConnection):
    """Ask the switch which mroutes it knows, then parse the output and print it.
    
    I still need to decide whether this functionality really belongs in a separate class, perhaps in the mroute_client_session.py file.
    """
    chan, session = await conn.create_session(IPMrouteClientSession, term_type="vt100", term_size=(80,100000))  # We need a bajillion lines, to prevent the switch from doing a `more` kind of thing and getting the program stuck.
    chan.write("show ip mroute\r\n")
    await asyncio.sleep(2)
    chan.write("exit\r\n")
    await chan.wait_closed()
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


async def run_client(host: str):
    """Run the ssh client.

    We create (currently) two concurrent sessions, one to scrape the mroutes, the other is basically a placeholder
    which prints out the version info of the switch.
    """
    async with asyncssh.connect(host=host, username="monitor", password="monitor") as conn:
        # Creating the task first, then awaiting it, allows multiple tasks to run concurrently, as opposed to
        # completely finishing the first one, then only starting with the next.
        mroute_task = asyncio.create_task(scrape_mroutes(conn))
        version_task = asyncio.create_task(get_version(conn))
        await version_task 
        await mroute_task

                

if __name__ == "__main__":

    description = """This program connects to Mellanox switches via SSH and queries the mroutes known to the swith.
                     It then prints a summary of the multicast groups going out on each poirt.
                  """
    parser = OptionParser(description=description)
    parser.set_usage("%prog [options]")
    parser.add_option("-a", "--addr", dest="addr", type=str, default="10.8.96.57",
                      help="Hostname or ip address of Mlnx switch to contact.")
    opts, _args = parser.parse_args()

    try:
        asyncio.get_event_loop().run_until_complete(run_client(host=opts.addr))
    except (OSError, asyncssh.Error) as exc:
        sys.exit('SSH connection failed: ' + str(exc))