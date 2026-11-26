# Releasing

The planner decides when to release. Merging the packaging PR and pushing the tag
are the only manual steps; GitHub Actions does the rest.

## 1. Packaging PR

```sh
python3 scripts/build_fork_release.py X.Y.Z.W
```

This writes the version into `src/zap2xml/plugin.py` and `src/zap2xml/plugin.json`,
rewrites `manifest.json` and `zips/zap2xml/manifest.json` with the new zip's md5,
sha256 and size, and builds `dist/zap2xml-vX.Y.Z.W.zip` (`dist/` is untracked).
Commit the four tracked files and open a PR. CI (`.github/workflows/ci.yml`) runs
the tests and rebuilds the zip with `--check`, which fails if the committed zip's
checksum is not reproducible.

## 2. Tag

Once the packaging PR is merged, tag the merged commit on `main` and push the tag:

```sh
git tag -a vX.Y.Z.W -m "Release X.Y.Z.W: <one line>" <main sha>
git push origin vX.Y.Z.W
```

Use an annotated tag: its message becomes the release notes. Without a message the
workflow falls back to GitHub's generated notes.

## 3. What the release workflow does

`.github/workflows/release.yml` runs on any `v*` tag and holds `contents: write`
(the only workflow that does; CI is `contents: read`). It stops before publishing
unless all of these hold:

- the tagged commit is an ancestor of `origin/main` (`git merge-base --is-ancestor`);
- the tag is exactly `v` plus the version in `src/zap2xml/plugin.json`;
- both `manifest.json` and `zips/zap2xml/manifest.json` list that version;
- the unit tests pass;
- the zip rebuilt from the tagged tree has the sha256 the manifests advertise;
- no release exists for the tag yet.

Then it:

1. creates the release for the tag with `zap2xml-vX.Y.Z.W.zip` attached (not a draft);
2. downloads the published asset and checks its sha256 again;
3. commits the tagged tree onto the `tufanito` branch, the one Dispatcharr's registry
   reads, with the message `Advertise Zap2XML X.Y.Z.W in the Dispatcharr registry`.
   The push is a plain fast-forward: never forced, and it aborts if `tufanito` moved
   while the job ran. It then verifies the tree matches the tag and that the registry
   serves the new version;
4. writes a job summary with the version, sha256, release URL and `tufanito` commit.

## 4. Recovering from a partial run

Each step is safe to repeat by hand; there is no need to delete the tag.

- **It failed before `Create the GitHub release`.** Fix the cause (usually a manifest
  or checksum mismatch), then either push the corrected commit and move the tag, or
  run the release by hand.
- **The release exists but `tufanito` was not updated** (the registry step failed).
  Finish the registry step locally, never by force-pushing:

  ```sh
  git fetch origin
  git checkout -B tufanito origin/tufanito
  git read-tree -u --reset vX.Y.Z.W
  git commit -m "Advertise Zap2XML X.Y.Z.W in the Dispatcharr registry"
  git diff --quiet vX.Y.Z.W tufanito && git push origin tufanito
  ```

  Re-running the workflow will not help here: it refuses to touch a tag whose release
  already exists.
- **The release itself is wrong.** Delete it, fix the tag, and push again. Releases
  are planner-owned; delivery never publishes or deletes them.

## Checking a package locally

```sh
python3 scripts/build_fork_release.py X.Y.Z.W --check          # rebuild in a temp dir and compare
python3 scripts/build_fork_release.py X.Y.Z.W --out dist --check  # same, keeping the zip
```

Both modes leave every tracked file alone. `--check` skips the comparison when no
manifest lists the version yet, so it is safe on a branch that has not been packaged.
