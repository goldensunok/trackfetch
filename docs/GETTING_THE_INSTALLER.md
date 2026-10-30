# How to get TrackfetchSetup.exe

Read this once, follow it once, and you will have a proper Windows installer.
After that you never need to look at it again unless you want a new version.

**Why this step exists.** A Windows program has to be built on Windows. It was
built and tested on Linux, so the finished `.exe` has to be produced somewhere
running Windows. GitHub gives you a Windows machine for free and everything on
it is button clicking. There is no typing of commands anywhere below.

It takes about ten minutes, most of which is waiting.

---

## Step 1. Get a free GitHub account

1. Go to <https://github.com/signup>
2. Enter an email address, a password and a username.
3. Confirm your email address when the message arrives.

Free is enough. You do not need to pay for anything.

---

## Step 2. Make a place to put the code

1. Once signed in, go to <https://github.com/new>
2. In **Repository name**, type: `trackfetch`
3. Choose **Private**. Nobody else needs to see it.
4. Leave everything else alone.
5. Click **Create repository**.

You now have an empty page with some instructions on it. Ignore the
instructions.

---

## Step 3. Put the Trackfetch files on it

1. Unzip `trackfetch.zip` somewhere you can find it, for example your Desktop.
   You will end up with a folder called `trackfetch`.
2. On the GitHub page, click the link **uploading an existing file**.
   (If you cannot see it: click **Add file** at the top right, then
   **Upload files**.)
3. Open the unzipped `trackfetch` folder in Windows Explorer.
4. Select everything inside it: press **Ctrl and A**.
5. Drag it all onto the dashed box on the GitHub page.
6. Wait for the uploads to finish. There are a few hundred files, so give it a
   minute. A progress line appears at the bottom.
7. Scroll down and click the green **Commit changes** button.

### If the `.github` folder did not upload

Windows sometimes hides folders whose name starts with a dot, and GitHub
occasionally skips them. Check: on your repository page, do you see a folder
called `.github` in the list?

If not, do this instead. It is still just typing and pasting:

1. Click **Add file**, then **Create new file**.
2. In the file name box, type exactly:
   `.github/workflows/build-windows.yml`
   GitHub will turn the slashes into folders as you type. That is correct.
3. Open `.github/workflows/build-windows.yml` from your unzipped folder in
   Notepad, select all of it, copy it.
4. Paste it into the big box on GitHub.
5. Click **Commit changes**, then **Commit changes** again.

---

## Step 4. Build the installer

1. Click the **Actions** tab along the top of your repository.
2. If GitHub asks whether to enable workflows, click the green
   **I understand my workflows, go ahead and enable them**.
3. On the left you will see **Build Trackfetch for Windows**. Click it.
4. On the right there is a **Run workflow** button. Click it, then click the
   green **Run workflow** in the little box that drops down.
5. Wait. Refresh the page after a moment and you will see a run appear with a
   spinning yellow dot. It takes roughly five to eight minutes.
6. When both ticks go green, the build is finished.

If a red cross appears instead, click into the run and click the step that
failed. The message will say what went wrong. Send it on and it can be fixed.

---

## Step 5. Download it

1. Click on the finished run.
2. Scroll to the bottom of that page, to a section called **Artifacts**.
3. Click **TrackfetchSetup**. A zip file downloads.
4. Open the downloaded zip and drag `TrackfetchSetup.exe` out of it onto your
   Desktop.

That file is the installer. Double click it and follow
`README_FOR_TOBIAS.txt` from step 1.

GitHub keeps the artifact for 90 days. After that, just press **Run workflow**
again to get a fresh one.

---

## Getting a new version later

If any of the Trackfetch files are ever changed, upload the changed ones the
same way and the build starts again by itself. Then download the new
`TrackfetchSetup` from the Actions tab exactly as above.

---

## A note on the Windows warning

The installer is not code signed, because that needs a paid certificate from a
certificate authority. Windows will therefore show a blue **"Windows protected
your PC"** box the first time you run it.

Click **More info**, then **Run anyway**. This is normal for any new program
without a certificate, and it stops appearing once Windows has seen the file a
few times.
