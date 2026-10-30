===============================================================================
TRACKFETCH
Read me first
===============================================================================

Trackfetch finds and downloads files from several different places and keeps
them all in one list. The main one is Soulseek.

You do not need to know anything about programming to use it. Nothing in this
file asks you to open a terminal or type a command.

Everything below is in the order you will actually need it.


-------------------------------------------------------------------------------
1. WHICH FILE DO I OPEN TO INSTALL IT?
-------------------------------------------------------------------------------

Open the file called:

    TrackfetchSetup.exe

Double click it. That is the only file you need.

Windows may show a blue box saying "Windows protected your PC". This happens
with any new program that has not paid for a code signing certificate. Click
"More info", then click "Run anyway".


-------------------------------------------------------------------------------
2. WHAT DO I CLICK DURING INSTALLATION?
-------------------------------------------------------------------------------

    1. Next
    2. Next  (or choose a different folder first, if you want to)
    3. Leave "Put a Trackfetch shortcut on my desktop" ticked, then Next
    4. Install
    5. Leave "Open Trackfetch now" ticked, then Finish

Trackfetch then opens on its own. Afterwards you can start it from the desktop
shortcut or from the Start Menu.


-------------------------------------------------------------------------------
3. WHAT IS SLSKD, IN ONE SENTENCE?
-------------------------------------------------------------------------------

slskd is a small background program that talks to the Soulseek network on
Trackfetch's behalf, doing the actual searching and downloading.


-------------------------------------------------------------------------------
4. DOES TRACKFETCH INSTALL AND SET UP SLSKD AUTOMATICALLY?
-------------------------------------------------------------------------------

Yes. The first time you open Trackfetch, a short setup guide appears. On the
Soulseek page you type your Soulseek username and password and press
"Set up Soulseek now". Trackfetch then does all of this by itself:

    - checks whether the helper is already on your computer
    - downloads the official helper if it is not
    - writes its settings
    - starts it
    - waits for it to be ready
    - signs in
    - checks that Soulseek itself is connected

You watch the steps tick past in the box. It normally takes under a minute.

Trackfetch keeps the helper entirely inside its own folder. It does not touch,
change, move or remove the ordinary Soulseek program you already use.


-------------------------------------------------------------------------------
5. WHAT IF THE AUTOMATIC SETUP DOES NOT WORK?
-------------------------------------------------------------------------------

Press "Set up Soulseek now" a second time. Most first time failures are
security software being cautious about a brand new program, and it works on the
second attempt.

If it still will not work:

    a. Go to https://github.com/slskd/slskd/releases/latest
    b. Download the file whose name ends in  win-x64.zip
    c. Right click it, choose "Extract All", and put the contents in a folder
       called slskd inside your user folder, for example
       C:\Users\Tobias\slskd
    d. Open Trackfetch, go to Settings, then Connections, then Soulseek,
       then Configure, and press Test Connection

Trackfetch looks in that folder and will find it.


-------------------------------------------------------------------------------
6. WHAT SOULSEEK LOGIN DETAILS DO I NEED?
-------------------------------------------------------------------------------

Your ordinary Soulseek username and password. The same ones you type into the
Soulseek program you already use.

IMPORTANT: Soulseek only allows one login per account at a time. If the normal
Soulseek program is open at the same time as Trackfetch, one of them will be
knocked off. You have two choices:

    - close the normal Soulseek program while you use Trackfetch, or
    - make a second free Soulseek account and use that one in Trackfetch

Trackfetch will tell you in plain words if it notices this happening.


-------------------------------------------------------------------------------
7. WHERE DO I TYPE THEM IN?
-------------------------------------------------------------------------------

In the setup guide when Trackfetch first opens.

Later on: Settings, then Connections, then Soulseek, then Configure.

Your password is stored in Windows' own password store, not in a plain file,
and it never appears in any log or in the diagnostic information.


-------------------------------------------------------------------------------
8. HOW DO I KNOW SOULSEEK IS CONNECTED?
-------------------------------------------------------------------------------

At the top of the main window there is a row called Connections. When it is
working it says:

    Soulseek: Connected

in green with a tick. If it says anything else, there will be a line of plain
English underneath telling you what to do about it.

You can press "Check now" at any time to check again.


-------------------------------------------------------------------------------
9. HOW DO I SET UP ANOTHER SERVICE?
-------------------------------------------------------------------------------

Settings, then Connections. You will see a list:

    Soulseek        Web download     qBittorrent     Transmission
    Deluge          JDownloader      aria2           SFTP
    WebDAV          Syncthing        NZBGet          SABnzbd

For each one there is a tick box to switch it on, a Configure button, and a
Test Connection button. Configure asks only for the few things it needs, and
tells you where to find them in that program. Anything you can safely ignore is
hidden behind "Advanced settings".

Only switch on the ones you actually use. Everything switched off stays out of
your way and does not appear on the main screen.

Web download needs no setup at all and is already on.


-------------------------------------------------------------------------------
10. HOW DO I PASTE A SEARCH LIST?
-------------------------------------------------------------------------------

Click the tab "1. Your list" and paste into the big box. One item per line.

Anything at all works as a search. It does not have to be music. For example:

    Aphex Twin - Xtal
    Burial - Archangel
    linux mint iso
    annual report 2025 pdf

Lines starting with # are ignored, so you can leave yourself notes.

Hundreds of lines at a time are fine.


-------------------------------------------------------------------------------
11. HOW DO I IMPORT A TXT OR CSV FILE?
-------------------------------------------------------------------------------

On the same tab, click "Import TXT or CSV" and choose your file.

For a text file, each line becomes one item.

For a spreadsheet saved as CSV, Trackfetch looks for a column called Query,
Search, Title, Track, Name, URL or similar and uses that. If there is no such
column it uses the first one. The other columns are kept as notes.


-------------------------------------------------------------------------------
12. HOW DO I ADD A WEB LINK?
-------------------------------------------------------------------------------

Click "Add web link", paste the address, click OK. You can also just paste web
addresses straight into the big box along with everything else.


-------------------------------------------------------------------------------
13. HOW DO I ADD A TORRENT OR A MAGNET LINK?
-------------------------------------------------------------------------------

Click "Add torrent or magnet" and choose either "A torrent file on this
computer" or "A magnet link".

You need one of qBittorrent, Transmission or Deluge switched on in Settings,
because Trackfetch hands torrents to those programs rather than doing the
torrent work itself. If none is set up, Trackfetch says so plainly.


-------------------------------------------------------------------------------
14. HOW DO I ADD AN NZB?
-------------------------------------------------------------------------------

Click "Add NZB" and choose the file.

You need NZBGet or SABnzbd switched on in Settings, for the same reason.


-------------------------------------------------------------------------------
15. HOW DO I SEARCH SOULSEEK?
-------------------------------------------------------------------------------

Press the big blue SEARCH ALL button.

Trackfetch searches the real Soulseek network for every item on your list, one
after another, several at a time. A counter shows the progress:

    Searching 17 / 126

Searching downloads nothing. It only collects the results.

You can press "Stop searching" at any point and keep whatever has come back so
far.


-------------------------------------------------------------------------------
16. HOW DO I FILTER THE RESULTS?
-------------------------------------------------------------------------------

Go to the tab "2. Results". Along the top there are filters:

    File type, Bitrate, Sample rate, Bit depth, Size, Length,
    Name contains, Match at least, free slot only, hide locked files

Every one starts at Any. Trackfetch never prefers one format, bitrate, release
or version over another on your behalf, because that is your decision. The
filters do exactly what you set and nothing more.

Underneath the filters there is always a line in words saying what is currently
being filtered out, so a missing result is never a mystery.

"Keep results that do not report their details" is ticked by default. Many
people on Soulseek do not report a bitrate or a length, and unticking it would
hide perfectly good files.

If you want to change the search words for one item, right click it and choose
"Change the search words and search again". You can also skip an item from the
same menu.


-------------------------------------------------------------------------------
17. HOW DO I SELECT A RESULT?
-------------------------------------------------------------------------------

Tick the box next to it. You can tick as many as you like, across as many items
as you like.

Nothing is ever ticked automatically, and nothing downloads because the software
thinks it is the best match.

You can also highlight several rows, right click, and choose "Tick the
highlighted files".


-------------------------------------------------------------------------------
18. HOW DO I START DOWNLOADING?
-------------------------------------------------------------------------------

Press the big green DOWNLOAD SELECTED button. It shows how many files are
ticked.

Only the ticked files are fetched.


-------------------------------------------------------------------------------
19. HOW DO I SEE PROGRESS?
-------------------------------------------------------------------------------

Trackfetch moves to the tab "3. Downloads" by itself. Each row shows the file
name, which service is doing the work, the status, a progress bar, the size, how
much has arrived, the speed, the time left, where it is being saved, and a plain
English message if something went wrong.

Statuses are: Waiting, Queued, Downloading, Paused, Complete, Failed, Cancelled.

On Soulseek, "Queued" often means you are waiting in a queue on someone else's
computer. That is normal and it can take a while.


-------------------------------------------------------------------------------
20. WHERE DO FINISHED FILES APPEAR?
-------------------------------------------------------------------------------

In your download folder. It is shown at the bottom right of the window, and
there is an "Open download folder" button on the Downloads tab.

You chose the folder during setup. To change it: File, then "Choose download
folder", or Settings, then Downloads.


-------------------------------------------------------------------------------
21. HOW DO I PAUSE, RETRY OR CANCEL?
-------------------------------------------------------------------------------

On the Downloads tab, click a row and use the buttons along the bottom:

    Pause, Resume, Retry, Cancel, Remove from list

A button is greyed out when it does not apply, or when that service cannot do
it. Soulseek, for instance, cannot pause a download in progress, so Pause stays
greyed out for Soulseek rows. Cancel and Retry both work.

There is also "Retry everything that failed", which is useful after a long
batch, and "Clear finished" to tidy the list up.

A failure on one file never stops the others.


-------------------------------------------------------------------------------
22. HOW DO I CARRY ON AFTER CLOSING TRACKFETCH?
-------------------------------------------------------------------------------

Just open it again. Your list, your search results, your ticks and your download
queue are all still there, and anything that was still going is picked back up.

Anything already recorded as finished is not fetched a second time. If you
genuinely want it again, the message on screen tells you so at the time.

If you close Trackfetch while downloads are running it asks first, and explains
that they will be picked up next time.


-------------------------------------------------------------------------------
23. WHAT IF SOULSEEK SAYS DISCONNECTED?
-------------------------------------------------------------------------------

Work down this list, stopping when it goes green:

    1. Click "Check now" at the top right of the Connections row.
    2. Read the line of text underneath. It says what to do.
    3. If the ordinary Soulseek program is open, close it and check again.
       Soulseek only allows one login per account at a time.
    4. Settings, Connections, Soulseek, Configure, Test Connection. This
       reinstalls and restarts the helper if it needs to.
    5. Check your Soulseek username and password are right by signing in with
       the ordinary Soulseek program, then close it again.
    6. Restart your computer. Occasionally the helper is left in a stuck state.
    7. Use Copy Diagnostic Information (see below) and send the result on.


-------------------------------------------------------------------------------
24. WHAT IF ANOTHER SERVICE SAYS DISCONNECTED?
-------------------------------------------------------------------------------

Almost always it means that program is not running, or its remote control is
switched off. Check in this order:

    1. Is that program actually open on this computer?
    2. Settings, Connections, that service, Test Connection. The message tells
       you what went wrong in plain words.
    3. Configure, and check the address, username, password or key. The
       Configure window tells you where to find each one inside that program.
       For example, qBittorrent's are under Tools, Options, Web UI.
    4. If you do not use that service, just untick "Switched on" and it will
       stop appearing.


-------------------------------------------------------------------------------
25. HOW DO I USE COPY DIAGNOSTIC INFORMATION?
-------------------------------------------------------------------------------

Help, then "Copy Diagnostic Information".

That copies a report to your clipboard. Paste it into your message with Ctrl
and V.

It contains the Trackfetch version, your version of Windows, which services are
switched on, what state each is in, recent errors and the non-secret settings.

It contains no passwords, no keys and no tokens. You can read it before you send
it: it is shown on screen when you copy it.


-------------------------------------------------------------------------------
A FEW THINGS WORTH KNOWING
-------------------------------------------------------------------------------

Trackfetch never overwrites a file without asking. If something with the same
name is already in your download folder, it asks whether to keep both, skip it,
or replace it. You can set a permanent answer in Settings, Downloads.

Soulseek downloads come from other people's home computers. Failures are normal
and not a sign that anything is broken. Pick a different result and try again.

The two buttons that matter are always visible, whichever tab you are on:
SEARCH ALL and DOWNLOAD SELECTED. Searching never downloads anything, and
downloading only ever fetches what you ticked.

===============================================================================
