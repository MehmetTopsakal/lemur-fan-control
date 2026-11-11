"""StatusNotifierItem and DBusMenu tray integration using the existing Gio runtime.

The desktop renders the icon and menu, including on Wayland. No AppIndicator
bindings or third-party Python packages are needed. Gio is imported only when
a GUI creates the tray, so CLI commands and headless tests remain independent.
"""

WATCHER = 'org.kde.StatusNotifierWatcher'
ITEM = 'org.kde.StatusNotifierItem'
MENU = 'com.canonical.dbusmenu'
ITEM_PATH = '/StatusNotifierItem'
MENU_PATH = '/Menu'

ITEM_XML = '''<node><interface name="org.kde.StatusNotifierItem">
  <property name="Category" type="s" access="read"/>
  <property name="Id" type="s" access="read"/>
  <property name="Title" type="s" access="read"/>
  <property name="Status" type="s" access="read"/>
  <property name="WindowId" type="i" access="read"/>
  <property name="IconThemePath" type="s" access="read"/>
  <property name="IconName" type="s" access="read"/>
  <property name="IconPixmap" type="a(iiay)" access="read"/>
  <property name="OverlayIconName" type="s" access="read"/>
  <property name="OverlayIconPixmap" type="a(iiay)" access="read"/>
  <property name="AttentionIconName" type="s" access="read"/>
  <property name="AttentionIconPixmap" type="a(iiay)" access="read"/>
  <property name="AttentionMovieName" type="s" access="read"/>
  <property name="ToolTip" type="(sa(iiay)ss)" access="read"/>
  <property name="ItemIsMenu" type="b" access="read"/>
  <property name="Menu" type="o" access="read"/>
  <method name="Activate"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
  <method name="SecondaryActivate"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
  <method name="ContextMenu"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
  <method name="Scroll"><arg type="i" direction="in"/><arg type="s" direction="in"/></method>
  <method name="ProvideXdgActivationToken"><arg type="s" direction="in"/></method>
  <signal name="NewToolTip"/>
</interface></node>'''

MENU_XML = '''<node><interface name="com.canonical.dbusmenu">
  <property name="Version" type="u" access="read"/>
  <property name="TextDirection" type="s" access="read"/>
  <property name="Status" type="s" access="read"/>
  <property name="IconThemePath" type="as" access="read"/>
  <method name="GetLayout"><arg type="i" direction="in"/><arg type="i" direction="in"/>
    <arg type="as" direction="in"/><arg type="u" direction="out"/>
    <arg type="(ia{sv}av)" direction="out"/></method>
  <method name="GetGroupProperties"><arg type="ai" direction="in"/><arg type="as" direction="in"/>
    <arg type="a(ia{sv})" direction="out"/></method>
  <method name="GetProperty"><arg type="i" direction="in"/><arg type="s" direction="in"/>
    <arg type="v" direction="out"/></method>
  <method name="Event"><arg type="i" direction="in"/><arg type="s" direction="in"/>
    <arg type="v" direction="in"/><arg type="u" direction="in"/></method>
  <method name="EventGroup"><arg type="a(isvu)" direction="in"/><arg type="ai" direction="out"/></method>
  <method name="AboutToShow"><arg type="i" direction="in"/><arg type="b" direction="out"/></method>
  <method name="AboutToShowGroup"><arg type="ai" direction="in"/>
    <arg type="ai" direction="out"/><arg type="ai" direction="out"/></method>
  <signal name="LayoutUpdated"><arg type="u"/><arg type="i"/></signal>
</interface></node>'''


class SystemTray:
    def __init__(self, on_show, on_stop, on_quit, on_unavailable):
        from gi.repository import Gio, GLib
        self.Gio, self.GLib = Gio, GLib
        self.on_show, self.on_stop, self.on_quit = on_show, on_stop, on_quit
        self.on_unavailable = on_unavailable
        self.available = False
        self.closed = False
        self.generation = 0
        self.revision = 1
        self.message = 'Monitoring'
        self.can_stop = False
        self.preview = False
        self.quitting = False
        self.bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self.registrations = []
        try:
            for path, xml in ((ITEM_PATH, ITEM_XML), (MENU_PATH, MENU_XML)):
                info = Gio.DBusNodeInfo.new_for_xml(xml).interfaces[0]
                self.registrations.append(self.bus.register_object(
                    path, info, self._method, self._property, None))
            self.subscription = self.bus.signal_subscribe(
                WATCHER, WATCHER, None, '/StatusNotifierWatcher', None,
                Gio.DBusSignalFlags.NONE, self._watcher_signal)
            self.watch = Gio.bus_watch_name_on_connection(
                self.bus, WATCHER, Gio.BusNameWatcherFlags.NONE,
                self._appeared, self._vanished)
        except Exception:
            self.close()
            raise

    def _appeared(self, _bus, _name, _owner):
        self.generation += 1
        generation = self.generation
        self.bus.call(WATCHER, '/StatusNotifierWatcher', WATCHER,
                      'RegisterStatusNotifierItem', self.GLib.Variant('(s)', (ITEM_PATH,)),
                      None, self.Gio.DBusCallFlags.NO_AUTO_START, 2000, None,
                      self._registered, generation)

    def _registered(self, bus, result, generation):
        try:
            bus.call_finish(result)
        except self.GLib.Error:
            return
        if not self.closed and generation == self.generation:
            self._check_host()

    def _check_host(self):
        self.bus.call(WATCHER, '/StatusNotifierWatcher', 'org.freedesktop.DBus.Properties',
                      'Get', self.GLib.Variant('(ss)', (WATCHER, 'IsStatusNotifierHostRegistered')),
                      self.GLib.VariantType.new('(v)'), self.Gio.DBusCallFlags.NO_AUTO_START,
                      2000, None, self._host_checked, self.generation)

    def _host_checked(self, bus, result, generation):
        try:
            available = bool(bus.call_finish(result).unpack()[0])
        except self.GLib.Error:
            available = False
        if not self.closed and generation == self.generation:
            self._set_available(available)

    def _watcher_signal(self, _bus, _sender, _path, _interface, signal, _parameters):
        if signal in ('StatusNotifierHostRegistered', 'StatusNotifierHostUnregistered'):
            self._check_host()

    def _vanished(self, *_):
        self.generation += 1
        self._set_available(False)

    def _set_available(self, available):
        was_available = self.available
        self.available = available
        if was_available and not available and not self.closed:
            self.on_unavailable()

    def _property(self, _bus, _sender, _path, interface, name):
        V = self.GLib.Variant
        if interface == MENU:
            return {'Version': V('u', 4), 'TextDirection': V('s', 'ltr'),
                    'Status': V('s', 'normal'), 'IconThemePath': V('as', [])}[name]
        properties = {
            'Category': V('s', 'Hardware'), 'Id': V('s', 'lemur-fan-control'),
            'Title': V('s', 'Lemur Fan Control'), 'Status': V('s', 'Active'),
            'WindowId': V('i', 0), 'IconThemePath': V('s', ''),
            'IconName': V('s', 'utilities-system-monitor'), 'IconPixmap': V('a(iiay)', []),
            'OverlayIconName': V('s', ''), 'OverlayIconPixmap': V('a(iiay)', []),
            'AttentionIconName': V('s', ''), 'AttentionIconPixmap': V('a(iiay)', []),
            'AttentionMovieName': V('s', ''), 'ItemIsMenu': V('b', False),
            'Menu': V('o', MENU_PATH),
            'ToolTip': V('(sa(iiay)ss)', ('utilities-system-monitor', [],
                                        'Lemur Fan Control', self.message)),
        }
        return properties[name]

    def properties(self, item, names=()):
        V = self.GLib.Variant
        if item == 0:
            result = {'children-display': V('s', 'submenu')}
        elif item == 3:
            result = {'type': V('s', 'separator')}
        else:
            labels = {1: 'Show window', 2: 'Stop preview' if self.preview else 'Stop & restore',
                      4: 'Quit & restore'}
            result = {'label': V('s', labels[item]), 'visible': V('b', True),
                      'enabled': V('b', not self.quitting and (item != 2 or self.can_stop))}
        return {name: value for name, value in result.items() if not names or name in names}

    def layout(self, parent, depth, names):
        children = []
        if parent == 0 and depth != 0:
            children = [self.GLib.Variant('(ia{sv}av)', (item, self.properties(item, names), []))
                        for item in (1, 2, 3, 4)]
        return parent, self.properties(parent, names), children

    def _event(self, item, event):
        if item not in (0, 1, 2, 3, 4):
            return False
        if event == 'clicked' and not self.quitting:
            action = {1: self.on_show, 2: self.on_stop if self.can_stop else None,
                      4: self.on_quit}.get(item)
            if action is not None:
                def invoke():
                    if not self.closed:
                        action()
                    return False
                # Reply before Quit unregisters the D-Bus objects.
                self.GLib.idle_add(invoke)
        return True

    def _method(self, _bus, _sender, _path, interface, method, parameters, invocation):
        V = self.GLib.Variant
        args = parameters.unpack()
        try:
            reply = None
            if interface == ITEM:
                if method in ('Activate', 'SecondaryActivate', 'ContextMenu'):
                    self._event(1, 'clicked')
            elif method == 'GetLayout':
                reply = V('(u(ia{sv}av))', (self.revision, self.layout(*args)))
            elif method == 'GetGroupProperties':
                ids, names = args
                reply = V('(a(ia{sv}))', ([(i, self.properties(i, names)) for i in (ids or range(5))],))
            elif method == 'GetProperty':
                reply = V('(v)', (self.properties(args[0])[args[1]],))
            elif method == 'Event':
                if not self._event(args[0], args[1]):
                    raise KeyError(args[0])
            elif method == 'EventGroup':
                reply = V('(ai)', ([e[0] for e in args[0] if not self._event(e[0], e[1])],))
            elif method == 'AboutToShow':
                reply = V('(b)', (False,))
            elif method == 'AboutToShowGroup':
                reply = V('(aiai)', ([], [i for i in args[0] if i not in range(5)]))
            invocation.return_value(reply)
        except (KeyError, ValueError, TypeError) as exc:
            invocation.return_dbus_error('com.canonical.dbusmenu.Error.InvalidArgument', str(exc))

    def update(self, message, can_stop=False, preview=False, quitting=False):
        if self.closed:
            return
        if message != self.message:
            self.message = message
            self.bus.emit_signal(None, ITEM_PATH, ITEM, 'NewToolTip', None)
        state = can_stop, preview, quitting
        if state != (self.can_stop, self.preview, self.quitting):
            self.can_stop, self.preview, self.quitting = state
            self.revision += 1
            self.bus.emit_signal(None, MENU_PATH, MENU, 'LayoutUpdated',
                                 self.GLib.Variant('(ui)', (self.revision, 0)))

    def close(self):
        self.closed = True
        self.available = False
        if hasattr(self, 'watch'):
            self.Gio.bus_unwatch_name(self.watch)
        if hasattr(self, 'subscription'):
            self.bus.signal_unsubscribe(self.subscription)
        for registration in self.registrations:
            self.bus.unregister_object(registration)
        self.registrations.clear()
