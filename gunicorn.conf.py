# gunicorn reads this file from the working directory on its own, so these
# settings apply whatever start command Render's dashboard holds.
#
# Threads: one process (the free instance has 512 MB, and the transit
# timetables take ~100 MB), but four requests at a time, so a slow request —
# the clock's calendar re-sync, a cold transit load — doesn't stall the map
# for everyone. The shared caches (transit, live data, events) hold locks.
#
# Timeout: the default 30 s killed any request that ran longer, mid-work.
threads = 4
timeout = 120
