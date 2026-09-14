# One interface, not twelve

The screens were built one at a time, and it showed. Thirteen pages, each with
its own navigation; three of them answering the same question in different
words; two competing stylesheets, one of which set its own page background, so
the home page ended in a beige band nobody had chosen; half the screens light
and half dark; two of them in English inside a Ukrainian product. A person who
wanted to make a game could not walk from one end of it to the other.

This is the design the screens now share, and the reasoning behind it.

## What a person is actually doing

Three things, in this order, and everything else is subordinate to them:

1. **My games** — the list, and the one action that starts a new one.
2. **This game** — what is happening to it, what it needs from me, what it has
   cost, who is working on it, and what I can change.
3. **Setting up** — who does the work, on what machine, with what access.

So the navigation is two items, identical on every screen: **Мої ігри** and
**Налаштування**. The operator console, the run detail and the sign-in page are
real and stay reachable — from the footer, because they are not what a person
came for. The language switch is two pills in the header, not links in the
middle of a sentence.

## One system

`app.css` holds it: colour, a type scale, a spacing scale, radii, one shadow,
and one vocabulary for state used by every chip, bar and note in the product.
Nothing in it is decoration. **Every token is there because two pages disagreed
about it.**

Four rules are declared once, so a new screen inherits them instead of
rediscovering them:

- text meets AA contrast at its size;
- every control a person points at is at least 24×24;
- focus is always visible;
- nothing is ever wider than the window.

Page files carry only what is particular to that screen. `studio.css` knows
about stages and roles; `games.css` knows about the five steps of the intake;
neither knows what a button looks like.

## The game screen

It is one page, and it answers the questions in the order they are asked:

- **the top** — the game's name, one sentence on what is happening right now, a
  bar showing how much of the plan is done, and **exactly one** main action,
  chosen by the situation: no source of execution yet → *set up who does the
  work*; nobody has delegated the game → *let the studio run it*;
- **what needs you** — the card is not there at all when nothing is waiting;
- **the plan**, by stage, in human words;
- **the money** — spent, reserved, the limit, and a bar showing the first two
  against the third, with the per-role table folded away;
- **who is working** — one line per role, in a grid, so eight of them read as
  one list;
- **the controls** — your name **once**, then delegation, pause, comment and
  continue. It used to ask for the same name in two separate places on the same
  screen.

A game is chosen from a list of your games, or named in the address. It used to
be a text box you had to type a mission key into.

## Settings

Forty settings over eight sections is a wall. Each section is now folded shut
and says in one line what is inside — *6 налаштувань · змінено 0* — so the page
is one screen and you open the one you came for. The page went from 8400 pixels
tall to 1700.

## Bugs this found

Not one of these was a matter of taste:

- a link styled as a button lost its colour to the generic link rule and painted
  accent on accent: **invisible text on a solid button**;
- `display: flex` and `display: grid` silently beat the `hidden` attribute, so
  blocks the page believed it had hidden were painted anyway;
- nothing set `box-sizing`, so a field with padding was wider than the card
  holding it;
- the page was wider than a 320px window, because a grid's minimum column was
  wider than the phone;
- footer text sat at 4.21:1 against 4.5:1 required, in 19px targets;
- **a page asked for in English came back in Ukrainian**, because the language
  was appended twice to `/api/i18n?lang=en`;
- the home page's link to AI access had been folded inside a disclosure, and was
  dropped entirely when that disclosure went.

## How it is verified

A real browser at 320 CSS px and at laptop width, in both languages, against the
accessibility suite (a WCAG 2.2 AA subset: language, title, one first-level
heading, a main landmark, a bypass link that is genuinely first in focus order,
no horizontal overflow, announced status, accessible names, contrast, target
size, visible focus). The studio screen, the home page, settings, credentials
and eligibility all keep their own browser tests.

## What is not done yet

- **`/operations` and `/login` are still in English and still dark.** They are
  the operator's tools and the sign-in page; they are next, not forgotten.
- **`/work`, `/hardware`, `/game-plan`, `/installation` and `/access-guide` have
  not moved to the system.** The two that shared the old home-page stylesheet
  keep it verbatim as `pages-legacy.css` so that migrating the home page could
  not quietly restyle them. That file is not to be extended; it is there to be
  deleted, one screen at a time.
- **There is no guided setup path yet.** Settings is legible now, but it still
  does not walk a person through access → AI → this PC in order.
