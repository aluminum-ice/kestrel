#!/usr/bin/python3
"""Just Works Bluetooth pairing agent for the kestrel onboarding window.

Registers an org.bluez.Agent1 with the NoInputNoOutput capability as the
default agent and accepts pairing requests until it is stopped. bt-onboard
runs it only while the onboarding window is open, so the window is the gate.
Runs with the system Python (apt's python3-dbus and python3-gi), not the venv.
"""

import signal
import sys

import dbus
import dbus.mainloop.glib
import dbus.service
from gi.repository import GLib

BLUEZ = "org.bluez"
AGENT_IFACE = "org.bluez.Agent1"
AGENT_PATH = "/org/kestrel_pi/agent"


def log(msg):
    print(msg, flush=True)


class Rejected(dbus.DBusException):
    _dbus_error_name = "org.bluez.Error.Rejected"


class Agent(dbus.service.Object):
    @dbus.service.method(AGENT_IFACE, in_signature="", out_signature="")
    def Release(self):
        log("agent released")

    @dbus.service.method(AGENT_IFACE, in_signature="o", out_signature="")
    def RequestAuthorization(self, device):
        log(f"accepting Just Works pairing from {device}")

    @dbus.service.method(AGENT_IFACE, in_signature="ou", out_signature="")
    def RequestConfirmation(self, device, passkey):
        log(f"accepting pairing from {device}")

    @dbus.service.method(AGENT_IFACE, in_signature="os", out_signature="")
    def AuthorizeService(self, device, uuid):
        log(f"authorizing service {uuid} for {device}")

    # PIN and passkey entry would need input this agent doesn't have.
    @dbus.service.method(AGENT_IFACE, in_signature="o", out_signature="s")
    def RequestPinCode(self, device):
        raise Rejected("PIN pairing is not supported")

    @dbus.service.method(AGENT_IFACE, in_signature="o", out_signature="u")
    def RequestPasskey(self, device):
        raise Rejected("passkey entry is not supported")

    @dbus.service.method(AGENT_IFACE, in_signature="os", out_signature="")
    def DisplayPinCode(self, device, pincode):
        pass

    @dbus.service.method(AGENT_IFACE, in_signature="ouq", out_signature="")
    def DisplayPasskey(self, device, passkey, entered):
        pass

    @dbus.service.method(AGENT_IFACE, in_signature="", out_signature="")
    def Cancel(self):
        log("pairing cancelled")


def main():
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    bus = dbus.SystemBus()
    Agent(bus, AGENT_PATH)
    manager = dbus.Interface(bus.get_object(BLUEZ, "/org/bluez"), "org.bluez.AgentManager1")
    manager.RegisterAgent(AGENT_PATH, "NoInputNoOutput")
    manager.RequestDefaultAgent(AGENT_PATH)
    log("pairing agent registered (NoInputNoOutput)")

    loop = GLib.MainLoop()

    def stop():
        try:
            manager.UnregisterAgent(AGENT_PATH)
        except dbus.DBusException:
            pass
        loop.quit()
        return GLib.SOURCE_REMOVE

    # GLib's handlers, not signal.signal: Python handlers don't run while the
    # main loop is blocked in C.
    GLib.unix_signal_add(GLib.PRIORITY_HIGH, signal.SIGTERM, stop)
    GLib.unix_signal_add(GLib.PRIORITY_HIGH, signal.SIGINT, stop)
    loop.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
