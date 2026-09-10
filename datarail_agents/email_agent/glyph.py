"""Proposing listings to the Glyph repository.

The receptionist cannot open a pull request, and that is by design: it holds a
deploy key that can push to Glyph and do nothing else. So it pushes a branch,
listings/<ref>, holding one file per listing, and Glyph's own listing-intake
workflow judges the branch and opens the pull request. The agent drafts; Glyph
decides.

Every git call lives here, so run.py can be read without knowing any of it.
"""

from __future__ import annotations

import json
import os
import subprocess

AUTHOR = ("-c", "user.name=DataRail receptionist", "-c", "user.email=contact@datarail.org")


class GlyphError(RuntimeError):
    """The Glyph checkout is missing, unreadable, or refused a push."""


class GlyphRepo:
    """A checkout of fvtale/glyph, made by the workflow with the deploy key."""

    def __init__(self, root: str):
        self.root = root
        registry = os.path.join(root, "public", "data", "venues.json")
        try:
            with open(registry, encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError) as error:
            raise GlyphError("cannot read the venue registry at " + registry + ": "
                             + str(error)) from error
        self.venues = {venue["id"]: venue for venue in data.get("venues", [])
                       if isinstance(venue, dict) and venue.get("id")}
        # Every proposal branches from the commit the workflow checked out, so
        # one email's files can never leak into the next email's branch.
        self.base = self._git("rev-parse", "HEAD").strip()

    @classmethod
    def open(cls, root: str | None) -> GlyphRepo | None:
        """The checkout at `root`, or None if there is not one.

        No checkout means the GLYPH_DEPLOY_KEY secret is not set yet. That
        switches listing intake off rather than failing the run: the rest of
        the receptionist has nothing to do with Glyph.
        """
        if not root or not os.path.isdir(os.path.join(root, ".git")):
            return None
        return cls(root)

    # ------------------------------------------------------------------

    def _git(self, *args: str) -> str:
        try:
            result = subprocess.run(
                ["git", "-C", self.root, *args],
                check=True, capture_output=True, text=True, timeout=120,
            )
        except subprocess.CalledProcessError as error:
            raise GlyphError("git " + " ".join(args[:2]) + " failed: "
                             + (error.stderr or error.stdout or "").strip()) from error
        except subprocess.TimeoutExpired as error:
            raise GlyphError("git " + " ".join(args[:2]) + " timed out") from error
        return result.stdout

    def already_proposed(self, branch: str) -> bool:
        """Whether this email's branch already exists on Glyph.

        A run that pushed and then crashed before filing the mail must not
        propose it a second time on the next run.
        """
        return bool(self._git("ls-remote", "--heads", "origin", branch).strip())

    def propose(self, branch: str, listings: list, subject: str, body: str) -> None:
        """Commit one file per listing onto a fresh branch and push it.

        Raises GlyphError on any failure, which leaves the email unread for the
        next run. Nothing is filed until the branch is safely on Glyph.
        """
        folder = os.path.join(self.root, "feed", "curated")
        self._git("checkout", "-q", "-B", branch, self.base)
        try:
            os.makedirs(folder, exist_ok=True)
            for listing in listings:
                path = os.path.join(folder, listing["id"] + ".json")
                with open(path, "w", encoding="utf-8") as handle:
                    json.dump(listing, handle, indent=2, ensure_ascii=False)
                    handle.write("\n")
            self._git("add", "--", "feed/curated")
            self._git(*AUTHOR, "commit", "-q", "-m", subject, "-m", body)
            self._git("push", "-q", "origin", branch + ":refs/heads/" + branch)
        finally:
            # Back to a clean base whatever happened, so the next proposal in
            # this run starts from exactly what the workflow checked out.
            self._git("checkout", "-q", "-f", "--detach", self.base)
            if os.path.isdir(folder):
                self._git("clean", "-q", "-f", "-d", "--", "feed/curated")
