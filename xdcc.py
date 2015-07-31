#!/usr/bin/env python3
"""
XDCC Downloader

Usage:
  xdcc.py [-h | --help]
  xdcc.py (<target> <bot> <id>) [ -f FILE | - ] [-n NICK] [options]

Options:
  --file FILE, -f FILE     The desired filename
  --nick NICK, -n NICK     The desired nickname
  --sender S               Who can send the dcc request. [Default: target]
                           Choices: [target, <name>, all]
  --channel C, -c C        The channel the bot should join before requesting
                           the pack.
  --timeout T, -t T        How long should we wait. (in s) [Default: 30]
  --verb V                 The command sent to the bot [Default: XDCC SEND]
  --id-prefix I            The prefix for the ID. [Default: #]
  --user-agent UA          [Default: XDCC.PY/0.0.1 (PYTHON-IRCLIB/12)]
  --force-response         Should the bot force the DCC-Response packets to
                           be sent?
"""
import time
import math
import struct
import select
import shutil
import getpass
import os.path
import argparse
from shlex import split
from subprocess import list2cmdline
from threading import Thread, Event

import irc.client


suffixes = ['B', 'KB', 'MB', 'GB', 'TB', 'PB']
def humansize(nbytes):
    if nbytes == 0:
        return '0 B'

    # Very unpythonic but as long as it works.
    i = 0
    while nbytes >= 1024 and i < len(suffixes)-1:
        nbytes /= 1024.
        i += 1

    f = ('%.2f' % nbytes).rstrip(".00")
    return '%s%s' % (f, suffixes[i])


class XDCCStopableReactor(irc.client.Reactor):

    def __init__(self, *args, **kwargs):
        super(XDCCStopableReactor, self).__init__(*args, **kwargs)
        self.stopped = Event()

    def process_forever(self, timeout=0.2):
        while not self.stopped.is_set():
            self.process_once(timeout=timeout)

    def stop(self):
        self.stopped.set()


class XDCCDownloadClient(irc.client.SimpleIRCClient):
    reactor_class = XDCCStopableReactor

    def __init__(self, target, cmd, file, options):
        super(XDCCDownloadClient, self).__init__()
        self.target = target
        self.cmd = cmd
        self.file = file

        self.options = options

        self.dcc = None
        self._exited = False
        self.received_bytes = 0
        self.size = -1

        self._bar_received_bytes = 0
        self._timeout_received_bytes = 0

        self.original_filename = ""

        self._last_str = ""
        self.warning = ""
        self._short_warning = True
        self._refresh_timeout = False

        self.success = None

    def on_nicknameinuse(self, conn, evt):
        self._write_message("Nickname already in use.")
        conn.nick(conn.get_nickname() + "_")

    def on_ctcp(self, conn, evt):
        cmd, *args = evt.arguments
        if cmd == "VERSION":
            self.connection.ctcp_reply(
                evt.source.nick, "VERSION " + self.options.user_agent
            )
        elif cmd == "DCC":
            self.do_dcc(conn, evt)
        else:
            self._termmsg(conn, evt.source, evt.arguments)
            self.connection.quit()

    def on_welcome(self, conn, evt):
        self._write_status("Waiting for connection.")
        if not self.options.channel:
            self._initiate()
        else:
            self.connection.join(self.options.channel)

    def on_join(self, conn, evt):
        self._initiate()

    def _initiate(self):
        if self.dcc is not None:
            return

        self._privmsg(self.target, self.cmd)
        self._request_time = time.time()
        t = Thread(target=self._await_timeout)
        t.setDaemon(True)
        t.start()
    def _privmsg(self, target, txt):
        if self.options.verbose == 1:
            self._write_message(target, "<", txt)
        self.connection.privmsg(self.target, self.cmd)

    def _await_timeout(self):
        time.sleep(self.options.timeout)
        if self.dcc is None:
            self._disconnect()
            return

        while self.dcc is not None:
            for i in range(max(int(self.options.timeout), 1)):
                time.sleep(1)

                # We were notified that we should reset the timeout
                # since we expect a timeout.
                if self._refresh_timeout:
                    self._refresh_timeout = False
                    break

                # Hey, we finished in one way or another. Kill myself.
                if self.dcc is None:
                    return

            # Yeah, we failed. Stop the client.
            if self.received_bytes == self._timeout_received_bytes:
                self._disconnect()
                self._write_status("Download timed out.")
                return

    def _termmsg(self, *args, **kwargs):
        import sys
        print(*args, file=sys.stderr, **kwargs)
    def _write_message(self, *args, **kwargs):
        _lmsg = self._last_str
        self._write_status("")
        self._termmsg(*args, **kwargs)
        self._write_status(_lmsg)

    def check_source(self, source, target=False):
        if target:
            if source.nick == self.target:
                return True

        # Ensure we have the correct sender.
        if self.options.sender != "all":
            nick = source.nick
            for name in self.options.sender.split(","):
                if name == "target":
                    if nick == self.target:
                        return True
                elif name == nick:
                    return True
            return False
        return True

    def do_dcc(self, conn, evt):
        if not self.check_source(evt.source):
            self._termmsg("Unknown DCC Source: " + str(evt))
            return

        payload = evt.arguments[1]
        cmd, fn, addr, port, sz = split(payload)
        if cmd != "SEND":
            self._termmsg("Unexpected DCC Command:", cmd)
            self.connection.quit()
            return

        self.original_filename = fn

        self.stream = self._get_stream_of_file(os.path.basename(fn))
        addr, port = irc.client.ip_numstr_to_quad(addr), int(port)
        self.dcc = self.dcc_connect(addr, port, "raw")
        self.size = int(sz)
        _thread = Thread(target=self._dlnotice)
        _thread.setDaemon(True)
        _thread.start()

    def _write_status(self, string):
        # Make sure the status line fits the screen.
        term_size = shutil.get_terminal_size((80,20))
        self._last_str = self._last_str[:term_size.columns]

        self._termmsg("\r" + (" "*len(self._last_str)), end="")

        if len(string) > term_size.columns:
            string = string[:term_size.columns-3] + "..."

        self._termmsg("\r" + string, end="")
        self._last_str = string

    def _dlnotice(self):
        while self.dcc is not None:
            pos = self.received_bytes/self.size
            pos = int(30*pos)

            posstr = (("="*pos)+">").ljust(30, " ")

            if self.warning:
                extra = ">> " + self.warning + " <<"
                if self._short_warning:
                    self.warning = ""
            else:
                extra = repr(self.original_filename)

            # Make sure the status line fits the screen.
            term_size = shutil.get_terminal_size((80,20))

            if term_size.columns > 100:
                # Calcculate speed meter.
                speed = " ---.--    "
                if self.received_bytes != 0:
                    byte_delta = self.received_bytes - self._bar_received_bytes
                    speed = " %8s/s"%humansize(byte_delta)
                    self._bar_received_bytes = self.received_bytes
            else:
                speed = ""

            # Generate the new one.
            string = "".join(("\r%8s/%8s"%(
                humansize(self.received_bytes),
                humansize(self.size)
            ),  speed, " [", posstr,  "] ", extra, " "))

            self._write_status(string)
            time.sleep(1)

    def on_dccmsg(self, conn, evt):
        data = evt.arguments[0]
        self.stream.write(data)
        self.stream.flush()
        self.received_bytes = self.received_bytes + len(data)

        # Make sure we can write to the socket without blocking.
        # Otherwise just silently drop the received_bytes notice.
        r,w,x = select.select([], [self.dcc.socket], [], 0)
        if self.options.force_response or (self.dcc.socket in w):

            blocks =  self.options.force_response and self.dcc.socket not in w

            # Send a warning message so the users get a warning if it
            # starts to block.
            if blocks:
                self._short_warning = False
                self._refresh_timeout = True               # Expect timeout
                self.warning = "Download my be stuck."

            self.dcc.send_bytes(struct.pack("!I", self.received_bytes))

            if blocks:
                self._short_warning = True
                self.warning = ""

        # Some peers do not drop connection after we received the file.
        if self.received_bytes == self.size:
            self.on_dcc_disconnect(conn, evt)

    def on_dcc_disconnect(self, conn, evt):
        if self.dcc is not None:
            self.stream.close()
            self._disconnect()
            self.dcc.disconnect()
            self.dcc = None

        if not self._exited:
            if self.received_bytes == self.size:
                self._write_status("Download complete")
            elif not self.original_filename:
                self._write_status(
                    "Failed to download: " + self.original_filename
                )
            else:
                self._write_status(
                    "Failed to establish DCC connection."
                )
            self._termmsg()
            self._exited = True

    def on_disconnect(self, conn, evt):
        self.on_dcc_disconnect(conn, evt)
        self.success = self.received_bytes == self.size
        self._termmsg()
        self.stop()
    def stop(self):
        self.reactor.stop()
    def _get_stream_of_file(self, orig):
        if self.file == "-":
            import sys
            return sys.stdout.buffer
        elif not os.path.isdir(self.file):
            return open(self.file, "wb")
        else:
            path = os.path.join(self.file, orig)
            return open(path, "wb")

    def on_privmsg(self, conn, evt):
        if self.check_source(evt.source, target=True):
            self._write_message(evt.source.nick, ">", *evt.arguments)
    on_privnotice = on_privmsg
    def _disconnect(self):
        if not self._exited:
            self.connection.quit()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "server",
        help="The server address."
    )
    parser.add_argument(
        "bot",
        help="The name of the xdcc bot."
    )

    parser.add_argument(
        "id",
        help="The id of the pack."
    )

    parser.add_argument(
        "-o", "--output",
        required=False,
        default=".",
        action="store",
        help="The desired filename (or - for stdout)"
    )

    parser.add_argument(
        "-b", "--batch",
        action="store_true",
        help="Batch download a file."
    )

    parser.add_argument(
        "-n", "--nick",
        required=False,
        default=getpass.getuser(),
        action="store",
        help="The desired nickname. [Default: The current username.]"
    )

    parser.add_argument(
       "-c", "--channel",
        required=False,
        default=None,
        action="store",
        help="The channel that should be joined."
    )

    parser.add_argument(
        "--id-prefix",
        required=False,
        default="#",
        action="store",
        help="The prefix for the id."
    )

    parser.add_argument(
        "--verb",
        required=False,
        default="XDCC SEND",
        action="store",
        help="The command that should be sent. [Default: XDCC SEND]"
    )
    parser.add_argument(
        "-v", "--verbose",
        action="count",
        help="The verbosity of the output."
    )

    parser.add_argument(
        "--user-agent",
        required=False,
        default="TERM-XDCC/0.0.1 IRC/12",
        action="store",
        help="The verion string that is queried by some servers.",
        dest="user_agent"
    )

    parser.add_argument(
        "-t", "--timeout",
        required=False,
        default=30,
        type=int,
        action="store",
        help="The timeout time."
    )

    parser.add_argument(
        "--force-response",
        action="store_true",
        default=False,
        help="Enforce DCC-Packet response. (Dangerous)",
        dest="force_response"
    )

    parser.add_argument(
        "-s", "--sender",
        action="append",
        help="Who is allowed to send the DCC response?"
    )

    args = parser.parse_args()

    server = args.server.split(":", 2)
    if len(server)==2:
        addr, port = server[0], int(server[1])
    else:
        (addr,), port = server, 6667

    if not args.sender:
        args.sender = ["target"]
    args.sender = ",".join(args.sender)

    try:
        if not args.batch:
            return int(not download(addr, port, args.nick, args, args.id))
        else:
            return int(not batch(addr, port, args.nick, args))
    except KeyboardInterrupt:
        return -1

def batch(addr, port, nick, args):
    if not os.path.isdir(args.output):
        import sys
        print("Batch output not a directory.")
        return False

    id_iterator_list = []
    for r in args.id.split(","):
        if "-" in r:
            start, stop = r.split("-")
            id_iterator_list.append(range(int(start), int(stop)+1))
        else:
            id_iterator_list.append((int(r),))

    for iter in id_iterator_list:
        for id in iter:
            if not download(addr, port, nick, args, str(id)):
                return False
    return True


def download(addr, port, nick, args, id):
    cl = XDCCDownloadClient(
        args.bot,
        args.verb + " " + args.id_prefix + id,
        args.output,
        args
    )
    cl.connect(addr, port, nick)
    try:
        cl.start()
    except KeyboardInterrupt:
        cl._disconnect()
        raise

    return cl.success


if __name__ == "__main__":
    import sys
    sys.exit(main())
