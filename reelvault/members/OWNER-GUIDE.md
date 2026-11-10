# ReelVault Members: owner guide

This guide is for you, the community owner. You don't need to code.
Members get their own guide: [MEMBER-GUIDE.md](MEMBER-GUIDE.md). Send them that link.

---

## 1. What ReelVault Members is

ReelVault Members is a website for the people in your Skool community. Each member:

1. Tells ReelVault who their customer is (links and 4 short questions).
2. Picks the Instagram accounts that already win in their niche (their "competitors").
3. Lets their own Chrome read those accounts' reels, slowly and safely.
4. Sees the **outliers**: reels that did far better than that creator's normal reel ("4.2x normal").
5. Learns **what works**: the visual formats, hooks, calls to action and sounds that show up in the outliers.
6. Gets **format playbooks**: one page per winning format, with how to shoot it, edit it and add sound.
7. Makes things they can use today: reel scripts with an editor brief, Meta ad scripts, a 30-day plan and a
   competitor report. One click on any reel: **Remake this**, **Use this format** or **Borrow this hook**.

What it is not:
- It does not post, like, follow, comment or send messages on Instagram. It only reads.
- It does not take payments. There are no plans and no billing.
- It does not sell AI. **Every member brings their own AI** (their Claude plan, an API key, or the desktop app).
  You never pay for their AI.
- It does not read Instagram from your server. Instagram is read only in each member's own Chrome, with their own
  login. Their Instagram password and cookies never leave their browser.

Who can get in: only people you let in. There is no public sign-up.

---

## 2. Host it

ReelVault Members runs on one always-on server that you rent. The full, click-by-click guide is
**[deploy/DEPLOY.md](../deploy/DEPLOY.md)**. Start there.

The short version:
1. Rent a small server that meets the **"Host must have"** box at the top of DEPLOY.md. For about 10 members:
   2 CPUs, 4 GB memory, 80 GB local disk (about 8 to 15 euros a month). For 20 to 30 members: 4 CPUs, 8 GB.
2. Point an address like `app.yourdomain.com` at the server.
3. Run `./deploy/setup.sh` on the server. It asks for the address and your email and does the rest.
   It shows your first admin password **once**. Save it.
4. Save the file `deploy/.env` in your password manager. It holds the key that locks members' AI keys.
   If you lose it, every member must paste their AI key again.
5. Sign in, then **change your password** (Settings > Account).

Want to try before you have a domain? DEPLOY.md has a "Try it before you have a domain" part with a free
Cloudflare tunnel. You need that tunnel (or your real domain) to test the Claude connector, because Claude connects
from the internet, not from your computer.

Keep it healthy:
- **Updates:** `./deploy/setup.sh update` (it backs up first).
- **Backups** run by themselves every day. Copy them off the server too (DEPLOY.md, "Backups").
  **Try one restore before members join.**
- **Health:** Admin > Health shows members, disk space, the work queue, seconds per reel and the last backup.

### Put the Chrome extension in the Chrome Web Store (start in week 1)

Members need the ReelVault Members extension in Chrome, Edge or Brave. The Web Store review can take days, so
start early.
1. On the server, in the ReelVault folder, run
   `python3 deploy/build_extension.py --server https://app.yourdomain.com`.
   It makes a ZIP in the `dist/` folder.
2. Open the Chrome Web Store developer dashboard, add a new item, upload the ZIP and choose **Unlisted**.
   Use `https://app.yourdomain.com/#privacy` as the privacy page.
3. When the store says yes, copy the store link. Add this line to `deploy/.env`:
   `EXTENSION_STORE_URL=https://chromewebstore.google.com/detail/...` (your real link), then run
   `./deploy/setup.sh update`. Members then see an **Add to Chrome from the Web Store** button.

Until then, members install the extension from a ZIP file. The app has a guide for that, and the
[member guide](MEMBER-GUIDE.md) has the steps. It works, but it is a few more clicks.

---

## 3. Let members in

Open **Admin** in the left menu. A number next to it means people wait for you.

### The community code (the main way in)
1. Go to **Admin > Invites**. The **Community code** box shows the code and the **sign-up page** address
   (it ends in `/#join`).
2. Post both in a Skool Classroom lesson that **only members can see** (hidden from logged-out visitors).
3. Press **I posted it** on the banner.

The code changes by itself every 30 days. The old one keeps working for 2 more days. You see the new code in a
banner when you sign in. Post it again and press **I posted it**.

Need a new code right now (for example, it leaked)? Press **Make a new code**. The old code stops at once.

### Single-use invite links (for one person)
In **Admin > Invites > Single-use invite links**, type a label (for example "Jane from the coaching call"), choose
how many uses and for how many days, and press **Make an invite link**. Send the link. It is shown only once.
People with an invite link get in at once, without waiting for you.

### The allowlist (emails you trust)
In **Admin > Invites > Allowlist**, paste emails, one per line, and press **Add to the allowlist**. People with these
emails still use the community code. If email is set up, they get a link and are in once they open it. If email is
not set up, they wait for you with an **On the allowlist** badge.

### Approve people
New sign-ups with the code wait for you (you can change this in Admin > Settings, but waiting is safer).
1. Open **Admin > Members**. Pick **Waiting for approval** in the list on the right.
2. Check the Skool profile link under their name. Is this person in your community?
3. Press **Approve** or **Say no**.

With email set up, you get a note for each new sign-up and they get "You're in" when you approve. Without email,
tell them yourself. Email is optional; DEPLOY.md, "Email", shows how to turn it on.

### Turn off people who left
- **Admin > Members > Check who is still in the community:** upload the member export from Skool (CSV), press
  **Check**, tick the people who left, and press **Turn off the ticked members**.
- Or press **Turn off** on one member. They are signed out everywhere. **Turn on** brings them back.
- Members who don't log in for 120 days are turned off by themselves (Admin > Settings).
- With Zapier you can add new Skool members to the allowlist by themselves (DEPLOY.md, "Zapier").

### Help a member
- **Forgot password and no email set up?** Admin > Members > **Make a reset link**. Send them the link.
- **Need to see their screen?** Ask them to turn on support access (Settings > Account). Then **Look at their
  account** appears next to their name. You can look, not change anything. Every look is logged and shown to them.
  It turns off by itself after the days they picked.

---

## 4. What members see

The left menu: **Home · Competitors · Reels · What works · Create · Outputs · Settings**, plus **Help**.

| screen | what it shows |
|---|---|
| Setup (5 steps) | 1 Your links and 1-2 role models · 2 Connect Chrome · 3 Connect your AI · 4 Four quick questions · 5 Your first look. Every step but approving the profile can be skipped. |
| Home | What to do next, new outliers, work in progress with "Ready in about N hours", and **Formats that work in your niche** (the format playbooks). |
| Competitors | Tracked accounts, "Find the top accounts in my niche", **Pull the next 100 reels**, **Check for new reels**, **Break down the 5 best reels**. |
| Reels | Every pulled reel with filters (outliers only, format, hook, sound, topic). A reel page shows the numbers, why it worked, the transcript, the sound cues on a timeline and weak spots. |
| What works | Formats to copy, what makes reels outliers, "You vs them", and banks of hooks, formats, calls to action, sounds and music. |
| Format playbook | One page per named format: what it is, why it works, how to shoot it, how to edit it, the sound, "Can you make this?", outlier examples and **Write 3 scripts in this format**. |
| Create | Reel scripts, Meta ads, 30-day plan, competitor report. |
| Outputs | Everything they made, with downloads (text, CSV, print to PDF). |
| Settings | Your AI, Chrome extension, Profile, Topics, Account (password, support access, download data, delete account). |

What members **never** see: other members' data. Each member's reels, outputs and profile are theirs alone.
You see them only with support access (above). With **Show me anonymous top formats across members** on
(Admin > Settings), Admin > Health shows formats that work across 5 or more members, with no names.

---

## 5. Limits you can change (Admin > Settings)

| setting | default | why |
|---|---|---|
| Competitors per member | 10 | Keeps pulls and server work fair. |
| Pulls per member per day | 5 | Keeps each member's Instagram safe. Extra pulls start the next day by themselves. |
| Reels a member can process per day | 150 | Video work is the heavy part. Shares the server fairly. |
| Outputs per member per day | 40 | Stops runaway use. |
| Storage per member | 4,000 MB | Keeps the disk from filling up. |
| Keep videos for | 7 days | Videos are big. Numbers, frames and breakdowns stay. |
| Normal reels studied per competitor | 25 | See "Sampling" below. |
| Who gets in with the community code | They wait for my approval | Safer. |
| Make a new community code every | 30 days | |
| Turn off members who don't log in for | 120 days | |
| Let the server read Skool pages when a member has no extension | off | See "Skool" below. |

The extension also has fixed safety limits that you can't raise: one Instagram request at a time, 4 to 9 seconds
between requests, at most 120 Instagram reads an hour, 400 a day, and 150 video downloads a day per member.

---

## 6. Risks, in plain words

**Instagram account safety (the big one).** Reading other accounts with a member's own login can go against
Instagram's terms. Instagram may slow the account down, ask the member to confirm it's them, or, rarely, limit the
account. ReelVault reads slowly, like a person scrolling, makes zero Instagram calls while it waits, and stops for
24 hours the moment Instagram pushes back. But the risk is not zero.
- Tell members to use a **second Instagram account** in a **separate Chrome profile** if their main account
  matters to their business. The app has a guide (Help > Read Instagram with a second account).
- Every member must confirm "ReelVault will read Instagram as @username" before anything is read.
- Private accounts are never read.

**Sampling.** ReelVault does not watch every reel. It ranks all reels on views, likes and comments, then watches
and tags the outliers and the top reels, plus about 25 normal reels per competitor picked across time. "What works"
compares the two groups. That is fast and fair, but on small accounts the numbers are rougher. Each new pull of
the same account adds more normal reels, so the numbers get better over time. (A "process every reel" switch exists
inside ReelVault, but members have no button for it yet.)

**Shares and saves.** Instagram hides saves, and usually shares, for accounts you don't own. ReelVault ranks on
views, likes and comments. The app says this wherever it matters.

**Sound and visual tags are new.** The sound detector and the visual measurements were tuned on test clips, not
real reels. Sound cues say "detected, check by ear". Members can fix a wrong tag on any reel page; their fix wins.

**AI limits.** Free AI keys (like Gemini's free tier) have small daily limits and are not offered in the EU, EEA,
UK or Switzerland. The Claude connector works in chats, so a member needs a few Claude chats to finish a first
round (the app says about how many). Nothing runs until the member opens Claude.

**Skool and LinkedIn.** The member's own Chrome reads their own Skool and LinkedIn pages, only when they click.
The server never reads LinkedIn. The server reading Skool is off by default because Skool's terms forbid scraping.

**Your server.** The data must sit on a local disk, with backups copied off the server. Keep `deploy/.env` safe.
Members' AI keys are locked with encryption; the key to unlock them is only in that file.

**The Chrome Web Store may say no or take long.** Members can then keep using the ZIP. Safari and Firefox can't run
the extension; those members can use everything except reading Instagram.

---

## 7. What to test first (before members get access)

Do these in this order. Each takes 10 to 30 minutes, plus waiting. Write down what you see.

1. **Week 1: Chrome Web Store.** Build the store ZIP and submit it as Unlisted (section 2). Note any questions they ask.
2. **Hosting.** Set up the server with DEPLOY.md. Run the bench step ("Measure your server"). Restore one backup.
3. **Instagram, on a second account.** Make a separate Chrome profile with a second Instagram account. Sign up as a
   test member, pair the extension, confirm the account, and pull 100 reels from 2 competitors. Check:
   - the views, likes and comments match the Instagram app;
   - Admin > Health > Instagram reading looks right;
   - leaving Chrome open for an hour makes no Instagram calls (the counter there stays still);
   - no warning appears on Instagram over 3 days of normal use.
4. **Next 100 and new reels.** The next day, press **Pull the next 100 reels**, then **Check for new reels**.
   No reel should show up twice.
5. **Videos.** Check that outlier reels get their video and a breakdown.
6. **Find accounts.** With 1-2 role models, try **Find more accounts like these** and **Find the top accounts in my
   niche**. Are the top 10 right?
7. **AI, each way.** One test member each: a free Gemini key, an Anthropic key, the **Claude connector** (on your
   real domain or the tunnel; follow the member guide word by word and take screenshots, because Claude's screens
   change), and the Windows desktop app. Note speed and any error. You can change the connector steps members read
   in **Admin > Copy** without a new release.
8. **Skool and LinkedIn.** Use **Read my links now** on your own pages. Check that the Classroom lesson with the code
   is hidden from logged-out visitors.
9. **Sound and formats.** Watch 10 outliers with their sound cue sheets, and check 30 reels' formats and the named
   format playbooks. Note what's wrong; those notes tune the detectors.
10. **Outputs.** Make one of each output for your own agency. Hand one editor brief to a real editor.
11. **Members.** Run **Check who is still in the community** with a real Skool export. Approve one real sign-up.
12. **Windows.** Install the desktop app update on a real Windows PC and check the old app still works.

Then invite 3 to 5 friendly members first, and grow from there.

---

## 8. Where things are

- Member guide: [MEMBER-GUIDE.md](MEMBER-GUIDE.md)
- Hosting: [deploy/DEPLOY.md](../deploy/DEPLOY.md), [deploy/hosts/vps.md](../deploy/hosts/vps.md),
  [deploy/hosts/paas-with-volume.md](../deploy/hosts/paas-with-volume.md)
- The full technical blueprint (for engineers): [DESIGN-SAAS.md](../DESIGN-SAAS.md)
