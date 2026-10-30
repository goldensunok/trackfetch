"""Plain-English help shown inside the application."""

INSTRUCTIONS = """\
1. Check the Connections line at the top. Soulseek should say Connected.

2. On the first tab, type or paste what you are looking for, one item per line.
   Anything at all works as a search. It does not have to be music.
   You can also use Import TXT or CSV, Add web link, Add torrent or magnet,
   and Add NZB.

3. Press SEARCH ALL. Trackfetch searches the real Soulseek network through its
   background helper. Nothing is downloaded at this point.

4. Go to the Results tab. Results are grouped under the item you asked for.
   Use the filters at the top to narrow them down. Every filter starts at Any,
   and Trackfetch never prefers one version or format over another. That is
   your choice.

5. Tick the files you want. You can tick more than one.

6. Press DOWNLOAD SELECTED. Only the ticked files are fetched.

7. Watch the Downloads tab. You can pause, resume, retry and cancel there,
   depending on what each service supports.

8. Finished files appear in your download folder. The folder is shown at the
   bottom right of the window, and there is an Open download folder button.

If anything says Disconnected, click Check now. If it stays disconnected, use
Help then Copy Diagnostic Information and send the result on.

If Soulseek will not connect, first close the ordinary Soulseek program if you
have it open. The Soulseek network only allows one login per account at a time.
"""

SLSKD_EXPLANATION = """\
slskd is a small background program that talks to the Soulseek network for
Trackfetch. Trackfetch tells it what to search for and what to fetch, and it
does the actual Soulseek work.

It is separate from the ordinary Soulseek program you already use. Trackfetch
will not touch, change or remove that program.

Because the Soulseek network only allows one login per account at a time,
please close the ordinary Soulseek program while you are using Trackfetch, or
use a second Soulseek account.
"""
