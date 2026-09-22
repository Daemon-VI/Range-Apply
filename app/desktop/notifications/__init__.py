"""Local desktop notifications (Increment 5).

Notifications are *derived* from existing rows — attempt transitions
(``application_events``), finished execution runs and ingested signals — and
are never stored in the database. A small local cursor file remembers what
was already announced; the in-process :class:`NotificationCenter` keeps the
recent ones for the control center; ``winotify`` shows a Windows toast when
it is installed. Notifications are informational only: nothing here can
start, retry or change an application.
"""
