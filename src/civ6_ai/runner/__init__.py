"""The unattended runner: a run queue, a stall watchdog and a record of every game.

Nothing here talks to the game. The steps that do (launch and load a save, play turns, recover
from a stall) are passed in, so tests use fakes and a later live slice supplies the real ones.
"""
