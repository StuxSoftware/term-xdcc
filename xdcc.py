#!/usr/bin/env python3
"""
XDCC Downloader

Usage:
  xdcc.py [-h | --help]
  xdcc.py (<target>) [<nick>] [ <file> | - ] -b BOT -i ID [options]

Options:
  --verb V              The command sent to the bot [Default: XDCC SEND]
  --id-prefix I         The prefix for the ID. [Default: #]
  --bot BOT, -b BOT
  --id ID, -i ID
  --sender S            Who can send the dcc request. [Default: target]
                        Choices: [target, <name>, all]
  --channel C, -c C     The channel the bot should join before requesting
                        the pack.
"""
import struct
import select
import getpass
from shlex import split
from subprocess import list2cmdline
from threading import Thread
import docopt
import irc.client


class OptionContainer(object):
    __slots__ = ("sender", "channel")
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

        self._exited = False
        self.received_bytes = 0

    def on_ctcp(self, conn, evt):
        cmd, *args = evt.arguments
        if cmd == "VERSION":
            self.connection.ctcp_reply(evt.source.nick, "VERSION xdcc.py 0.0.1")
        elif cmd == "DCC":
            self.do_dcc(conn, evt)
        else:
            self._termmsg(conn, evt.source, evt.arguments)
            self.connection.quit()

    def on_welcome(self, conn, evt):
        self._termmsg("Waiting for connection.")
        if self.options.channel is None:
            self._initiate()
        else:
            self.connection.join(self.options.channel)

    def on_join(self, conn, evt):
        self._initiate()

    def _initiate(self):
        self.connection.privmsg(self.target, self.cmd)
    def _termmsg(self, *args, **kwargs):
        import sys
        print(*args, file=sys.stderr, **kwargs)

    def do_dcc(self, conn, evt):
        # Ensure we have the correct sender.
        if self.options.sender != "all":
            nick = evt.source.nick
            for name in self.options.sender.split(","):
                if name == "target":
                    if nick == self.target:
                        break
                elif name == nick:
                    break
            else:
                self._termmsg("Detected request from unknown source: %s" % (
                    nick
                ))
                return

        payload = evt.arguments[1]
        cmd, fn, addr, port, sz = split(payload)
        if cmd != "SEND":
            self._termmsg("Unexpected DCC Command:", cmd)
            self.connection.quit()
            return

        import os.path
        self.stream = self._get_stream_of_file(os.path.basename(fn))
        addr, port = irc.client.ip_numstr_to_quad(addr), int(port)
        self.dcc = self.dcc_connect(addr, port, "raw")
        self.size = int(sz)
        self._thread = Thread(target=self._dlnotice)
        self._thread.start()

    def _dlnotice(self):
        import time
        import math

        while self.dcc is not None:
            pos = self.received_bytes/self.size
            pos = int(30*pos)

            posstr = (("="*pos)+">").ljust(30, " ")


            self._termmsg("\r%.2f/%.2f"%(
                self.received_bytes/1024/1024,
                self.size/1024/1024
            ), " [", posstr,  "] ", sep=" ", end="")
            time.sleep(1)

    def on_dccmsg(self, conn, evt):
        data = evt.arguments[0]
        self.stream.write(data)
        self.stream.flush()
        self.received_bytes = self.received_bytes + len(data)

        # Make sure we can write to the socket without blocking.
        # Otherwise just silently drop the received_bytes notice.
        r,w,x = select.select([], [self.dcc.socket], [], 0)
        if self.dcc.socket in w:
            self.dcc.send_bytes(struct.pack("!I", self.received_bytes))

        # Some peers do not drop connection after we received the file.
        if self.received_bytes == self.size:
            self.on_dcc_disconnect(conn, evt)

    def on_dcc_disconnect(self, conn, evt):
        self.stream.close()
        self.connection.quit()
        self.dcc.disconnect()
        self.dcc = None
        if not self._exited:
            self._termmsg("\nDownload complete.")
            self._exited = True

    def on_disconnect(self, conn, evt):
        import sys
        sys.exit(0)

    def _get_stream_of_file(self, orig):
        if self.file == "-":
            import sys
            return sys.stdout.buffer
        elif self.file:
            return open(self.file, "wb")
        else:
            self._termmsg("Downloading into:", orig)
            return open(orig, "wb")


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

    nick = args["<nick>"]
    if nick is None:
        nick = getpass.getuser()

    options = OptionContainer(
        sender = args["--sender"],
        channel = args["--channel"]
    )

    cl = XDCCDownloadClient(
        args["--bot"],
        args["--verb"] + " " + args["--id-prefix"] + args["--id"],
        args["<file>"],
        options
    )
    cl.connect(target, port, nick)
    cl.start()

if __name__ == "__main__":
    import sys
    sys.exit(main())
