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
import getpass
from shlex import split
from subprocess import list2cmdline
from threading import Thread
import docopt
import irc.client


class OptionContainer(object):
    __slots__ = (
        "sender", "channel", "timeout", "user_agent",
        "force_response"
    )
    def __init__(self, *args, **kwargs):
        kwargs.update(dict(zip(self.__slots__, args)))
        for k,v in kwargs.items():
            setattr(self, k, v)

    def __repr__(self):
        string = [
            "<",
            self.__class__.__name__,
        ]
        for k in self.__slots__:
            string.append(" ")
            string.append(k)
            string.append("=")
            string.append(repr(getattr(self, k)))

        string.append(">")
        return "".join(string)


class XDCCDownloadClient(irc.client.SimpleIRCClient):
    def __init__(self, target, cmd, file, options):
        super(XDCCDownloadClient, self).__init__()
        self.target = target
        self.cmd = cmd
        self.file = file

        self.options = options

        self.dcc = None
        self._exited = False
        self.received_bytes = 0
        self._timeout_received_bytes = 0

        self.original_filename = ""

        self.warning = ""
        self._short_warning = True
        self._refresh_timeout = False

    def on_nicknameinuse(self, conn, evt):
        self._termmsg("Nickname already in use.")
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
        self._termmsg("Waiting for connection.")
        if not self.options.channel:
            self._initiate()
        else:
            self.connection.join(self.options.channel)

    def on_join(self, conn, evt):
        self._initiate()

    def _initiate(self):
        self.connection.privmsg(self.target, self.cmd)
        self._request_time = time.time()
        t = Thread(target=self._await_timeout)
        t.setDaemon(True)
        t.start()

    def _await_timeout(self):
        time.sleep(self.options.timeout)
        if self.dcc is None:
            self.connection.quit()
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
                self.connection.quit()
                self._termmsg("\nDownload timed out.")
                return

    def _termmsg(self, *args, **kwargs):
        import sys
        print(*args, file=sys.stderr, **kwargs)

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

        import os.path
        self.stream = self._get_stream_of_file(os.path.basename(fn))
        addr, port = irc.client.ip_numstr_to_quad(addr), int(port)
        self.dcc = self.dcc_connect(addr, port, "raw")
        self.size = int(sz)
        _thread = Thread(target=self._dlnotice)
        _thread.setDaemon(True)
        _thread.start()

    def _dlnotice(self):
        self._last_str = ""
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

            self._termmsg("\r" + (" "*len(self._last_str)), end="")

            self._last_str = "".join(("\r%.2f/%.2f"%(
                self.received_bytes/1024/1024,
                self.size/1024/1024
            ), " [", posstr,  "] ", extra, " "))

            self._termmsg(self._last_str, end="")
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
        self.stream.close()
        self.connection.quit()
        self.dcc.disconnect()
        self.dcc = None
        if not self._exited:
            if self.received_bytes == self.size:
                self._termmsg("\nDownload complete.")
            else:
                self._termmsg()
            self._exited = True

    def on_disconnect(self, conn, evt):
        import sys
        sys.exit(0 if self.received_bytes == self.size else 1)

    def _get_stream_of_file(self, orig):
        if self.file == "-":
            import sys
            return sys.stdout.buffer
        elif self.file:
            return open(self.file, "wb")
        else:
            self._termmsg("Downloading into:", orig)
            return open(orig, "wb")

    def on_privmsg(self, conn, evt):
        if self.check_source(evt.source, target=True):
            self._termmsg(">", *evt.arguments)
    on_privnotice = on_privmsg


def main():
    args = docopt.docopt(__doc__)
    target = args.get("<target>", "")
    if target is not None:
        target = target.split(":")

    if target is None or len(target)>2:
        print("Invalid target.")
        return -1
    elif len(target)==2:
        target, port = target[0], int(target[1])
    else:
        (target,), port = target, 6667

    if args["-"]:
        args["--file"] = "-"

    nick = args["--nick"]
    if nick is None:
        nick = getpass.getuser()

    options = OptionContainer(
        sender = args["--sender"],
        channel = args["--channel"],
        timeout = int(args["--timeout"]),
        user_agent = args["--user-agent"],
        force_response = args["--force-response"]
    )

    cl = XDCCDownloadClient(
        args["<bot>"],
        args["--verb"] + " " + args["--id-prefix"] + args["<id>"],
        args["--file"],
        options
    )
    cl.connect(target, port, nick)
    try:
        cl.start()
    except KeyboardInterrupt:
        cl.on_dcc_disconnect(None, None)

if __name__ == "__main__":
    import sys
    sys.exit(main())
