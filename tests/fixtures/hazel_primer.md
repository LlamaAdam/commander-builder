<!--
Provenance: data["description"] of Archidekt deck 24864897
("Hazel demands Sacrifice", owner-provided, public), fetched 2026-08-20
from https://archidekt.com/api/decks/24864897/ via CI (sandbox egress
blocked). Kept for FP-016 Phase 1: this is a REAL stated-intent primer,
the test case the future deck judge has to read a player's intent out of.

hasPrimer: true. NOTE THE SHAPE: Archidekt's `description` is not plain
text — it is a Quill Delta JSON *string* ({"ops": [{"insert": ...}]}).
This capture is one op with no attributes, so the rendered text is a plain
`ops[0].insert`; a formatted primer would be many ops with `attributes`.
Anything that reads intent off `description` must parse the Delta, not
treat the field as prose.

Section 1 is the field VERBATIM (1,613 chars, byte-for-byte).
Section 2 is DERIVED from it (the insert string), for human reading.
-->

# Primer — "Hazel demands Sacrifice" (Archidekt deck 24864897)

## 1. `description` verbatim (Quill Delta JSON, as returned by the API)

```json
{"ops":[{"insert":"This started as the Precon \"Squirreled Away\"  but I wanted to add The Unbeatable Squirrel Girl too it and figured I ight want to lean in to making use of the tokens.... but also do away with the food package the deck had, as well as shore up the mana curve. And since it was already Golgari, and Chatterfang and the commander hazel each had a sacrifice theme, why not have death triggers in Zulaport CUttrhotoat, Marionette apprentice,  Arnyn Deathbloom Vengeful Broodwitch, Blood artist, Fiend Artisan, and everyone's favorite pirate, Pitiless Plunderer. Plunderer already goes infinite with Chatterfang... so why not have that feed any/all of the aforementioned Drain creatures? \nDoubling season is a grwat way to make eithe rof the planewalkers that much more fun upon entry, as it puts Garruk at 10 counters immediately, and lmpotant +3/+3 and Trample Emblem for all creatures. Turning all those (now doubled tokens. Hello, Squirrel Girl, we Love squirrels too!)) into 4/4 Tramplers.\n\nA game winning combo is\nSquirrel Girl, cryptolith rite, 4 squirrells, and concodant crossroads.  Have dryptolith and at least 4 squirrels on the board and Squirrel GIrl either in play or in hand, and Concordant crossroads in hand. Get Squirrel Girl in play, get Crossroads in play. Tap those squirrels for Squirrel Girl. Get 4 (or 8 if doubling season is out) Squirrells. They have haste (thanks crossroads) Tap 4 of those new tokens to her ability again. get 8 (or 16).  do this again and again and again.  Then, swarm the board. \n\nAll in all I had fun tinkering with this for a few days...\n"}]}
```

## 2. Rendered text (DERIVED from section 1 — not the raw field)

This started as the Precon "Squirreled Away"  but I wanted to add The Unbeatable Squirrel Girl too it and figured I ight want to lean in to making use of the tokens.... but also do away with the food package the deck had, as well as shore up the mana curve. And since it was already Golgari, and Chatterfang and the commander hazel each had a sacrifice theme, why not have death triggers in Zulaport CUttrhotoat, Marionette apprentice,  Arnyn Deathbloom Vengeful Broodwitch, Blood artist, Fiend Artisan, and everyone's favorite pirate, Pitiless Plunderer. Plunderer already goes infinite with Chatterfang... so why not have that feed any/all of the aforementioned Drain creatures? 
Doubling season is a grwat way to make eithe rof the planewalkers that much more fun upon entry, as it puts Garruk at 10 counters immediately, and lmpotant +3/+3 and Trample Emblem for all creatures. Turning all those (now doubled tokens. Hello, Squirrel Girl, we Love squirrels too!)) into 4/4 Tramplers.

A game winning combo is
Squirrel Girl, cryptolith rite, 4 squirrells, and concodant crossroads.  Have dryptolith and at least 4 squirrels on the board and Squirrel GIrl either in play or in hand, and Concordant crossroads in hand. Get Squirrel Girl in play, get Crossroads in play. Tap those squirrels for Squirrel Girl. Get 4 (or 8 if doubling season is out) Squirrells. They have haste (thanks crossroads) Tap 4 of those new tokens to her ability again. get 8 (or 16).  do this again and again and again.  Then, swarm the board. 

All in all I had fun tinkering with this for a few days...
