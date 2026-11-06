"""Tray-Prozess (GTK3 + AyatanaAppIndicator3). Start: ``python -m zeiterfassung.tray``.

Dieses ``__init__`` importiert bewusst kein Gtk, damit die GTK4-Haupt-App die
Konstanten nutzen kann, ohne GTK3 zu laden.
"""

TRAY_BUS_NAME = "de.local.Zeiterfassung.Tray"
