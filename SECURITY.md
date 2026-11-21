# Security policy

`wallclock` reads schedule specifications and the system tz database; it does not
execute user code, open sockets, or write outside the paths you name.

If you find a problem that could leak data (for example through the HTML report
or the `.ics` export), please use GitHub's **private vulnerability reporting**
("Security" tab -> "Report a vulnerability") instead of a public issue. If that is
unavailable, open a minimal issue that says a private report is needed, without
details.

Supported versions: the latest release receives fixes.
