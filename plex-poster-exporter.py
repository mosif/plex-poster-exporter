#!/usr/bin/env python3

import os
import sys
import datetime
import hashlib
import shutil
import subprocess
import tempfile

# sys
sys.dont_write_bytecode = True
if sys.version_info[0] < 3:
    print('\033[91mERROR:\033[0m', 'you must be running python 3.0 or higher.')
    sys.exit()

# click
try:
    import click
except ImportError:
    print('\033[91mERROR:\033[0m', 'click is not installed.')
    sys.exit()

# plexapi
try:
    import plexapi.utils
    from plexapi.server import PlexServer
    from plexapi.exceptions import BadRequest, NotFound
except ImportError:
    print('\033[91mERROR:\033[0m', 'plexapi is not installed.')
    sys.exit()

# tqdm (optional)
try:
    from tqdm import tqdm
    TQDM_AVAILABLE = True
except ImportError:
    TQDM_AVAILABLE = False

# defaults
NAME = 'plex-poster-exporter'
VERSION = '0.12'

VALID_EXPORT_TYPES = {'local', 'rclone'}


# plex
class Plex():
    def __init__(self, baseurl=None, token=None, library=None, force=False, verbose=False,
                 output_path=None, dry_run=False, mirror=False, cache_path=None,
                 export_types=None, rclone_dest=None, force_hash=False, rclone_config=None):
        self.baseurl = baseurl
        self.token = token
        self.server = None
        self.libraries = []
        self.library = library
        self.force = force
        self.force_hash = force_hash
        # --force-hash implies --mirror so the hash branch is reachable
        self.mirror = mirror or force_hash
        self.verbose = verbose
        self.output_path = output_path
        self.dry_run = dry_run
        self.cache_path = cache_path
        self.export_types = export_types or ['local']
        self.rclone_dest = rclone_dest
        self.rclone_config = rclone_config
        self.rclone_staging = None
        self.rclone_staged = 0
        self.downloaded = 0
        self.skipped = 0
        self.errors = 0

        # Validate export types
        invalid = [t for t in self.export_types if t not in VALID_EXPORT_TYPES]
        if invalid:
            print(f'\033[91mERROR:\033[0m invalid --export-type value(s): {", ".join(invalid)}. Valid: {", ".join(sorted(VALID_EXPORT_TYPES))}')
            sys.exit()
        if not self.export_types:
            print('\033[91mERROR:\033[0m at least one --export-type is required.')
            sys.exit()

        # Validate rclone availability if needed
        if 'rclone' in self.export_types:
            if not shutil.which('rclone'):
                print('\033[91mERROR:\033[0m rclone is not installed or not in PATH.')
                sys.exit()
            if not self.rclone_dest:
                print('\033[91mERROR:\033[0m --rclone-dest is required when --export-type includes rclone.')
                sys.exit()

        # Prepare cache directory if specified
        if self.cache_path:
            try:
                os.makedirs(self.cache_path, exist_ok=True)
            except Exception as e:
                print(f'\033[91mERROR:\033[0m could not create cache path {self.cache_path}: {e}')
                sys.exit()

        # Prepare a structured staging dir for the end-of-run bulk rclone copy.
        # Per-file rclone copyto causes duplicate folders on Google Drive due to
        # the API's eventual consistency; one bulk `rclone copy` at the end avoids
        # the race entirely AND is dramatically faster.
        if 'rclone' in self.export_types:
            staging_base = self.cache_path or tempfile.gettempdir()
            self.rclone_staging = os.path.join(staging_base, 'plex-poster-rclone-staging')
            # Wipe any leftover from a previous (possibly failed) run
            if os.path.exists(self.rclone_staging):
                shutil.rmtree(self.rclone_staging, ignore_errors=True)
            try:
                os.makedirs(self.rclone_staging, exist_ok=True)
            except Exception as e:
                print(f'\033[91mERROR:\033[0m could not create rclone staging dir {self.rclone_staging}: {e}')
                sys.exit()

        self.getServer()
        self.getLibrary()

    def getServer(self):
        try:
            self.server = PlexServer(self.baseurl, self.token)
        except BadRequest:
            print('\033[91mERROR:\033[0m', 'failed to connect to Plex. Check your server URL and token.')
            sys.exit()

        if self.verbose:
            print('\033[94mSERVER:\033[0m', self.server.friendlyName)

    def getLibrary(self):
        self.libraries = [_ for _ in self.server.library.sections() if _.type in {'movie', 'show'}]
        if not self.libraries:
            print('\033[91mERROR:\033[0m', 'no available libraries.')
            sys.exit()
        if self.library is None or self.library not in [_.title for _ in self.libraries]:
            self.library = plexapi.utils.choose('Select Library', self.libraries, 'title')
        else:
            self.library = self.server.library.section(self.library)
        if self.verbose:
            print('\033[94mLIBRARY:\033[0m', self.library.title)

    def getAll(self):
        return self.library.all()

    def getPath(self, item):
        """
        Use item.locations which Plex provides directly and accurately.
        For movies, locations contains file paths — return the parent directory.
        For shows and seasons, locations contains directory paths — return as-is.
        """
        if hasattr(item, 'locations') and item.locations:
            loc = item.locations[0]
            if self.library.type == 'movie':
                return os.path.dirname(loc)
            else:
                return loc
        return None

    def _relative_to_library(self, abs_path):
        """Strip the matching library root from abs_path to get a library-relative path.
        Returns None if abs_path isn't under any known library root."""
        if not hasattr(self.library, 'locations') or not self.library.locations:
            return None
        for root in self.library.locations:
            root_norm = root.rstrip('/')
            if abs_path == root_norm:
                return ''
            if abs_path.startswith(root_norm + '/'):
                return abs_path[len(root_norm) + 1:]
        return None

    @staticmethod
    def _files_equal(a, b):
        """Compare two files by size first, then SHA-256 if sizes match."""
        try:
            if os.path.getsize(a) != os.path.getsize(b):
                return False
            ha, hb = hashlib.sha256(), hashlib.sha256()
            with open(a, 'rb') as fa, open(b, 'rb') as fb:
                while True:
                    ca = fa.read(65536)
                    cb = fb.read(65536)
                    if not ca and not cb:
                        break
                    ha.update(ca)
                    hb.update(cb)
            return ha.hexdigest() == hb.hexdigest()
        except Exception:
            return False

    @staticmethod
    def _safe_remove(path):
        try:
            if path and os.path.exists(path):
                os.remove(path)
        except Exception:
            pass

    def _decide_local_action(self, local_path, source_updated_at):
        """Decide what to do for the local target.
        Returns 'download' (missing/force/Plex-newer), 'mirror' (hash-verify), or 'skip'.

        Mirror behavior tiers (cheapest first):
          - Plex's updatedAt is newer than local mtime → straight download, no hash.
          - --force-hash → hash-verify regardless of mtime (audit / post-restore).
          - Local mtime has drifted past Plex's updatedAt → hash-verify (catches Jellyfin
            overwrites, manual edits, etc.).
          - Otherwise local mtime matches Plex's expected state → skip without reading
            the file's bytes.

        We set local mtime = Plex updatedAt after every successful write, so the
        "matches" case skips on a stat alone — no NAS read of contents.
        """
        if not os.path.isfile(local_path):
            if self.verbose:
                print(f'  [MISSING] {local_path}')
            return 'download'
        if self.force:
            return 'download'

        grace = datetime.timedelta(seconds=60)
        local_mtime = None

        if source_updated_at:
            local_mtime = datetime.datetime.fromtimestamp(os.path.getmtime(local_path))
            if source_updated_at > (local_mtime + grace):
                if self.verbose:
                    print(f'  [PLEX NEWER] Plex: {source_updated_at} > Local: {local_mtime}')
                return 'download'

        # File exists, Plex isn't newer.
        if self.force_hash:
            if self.verbose:
                print(f'  [FORCE HASH] verifying {local_path}')
            return 'mirror'

        if self.mirror:
            if source_updated_at is None:
                return 'mirror'
            if local_mtime is not None and local_mtime > (source_updated_at + grace):
                if self.verbose:
                    print(f'  [LOCAL DRIFT] Local: {local_mtime} > Plex: {source_updated_at} — verifying hash')
                return 'mirror'

        return 'skip'

    def _download_to_cache(self, url, filename):
        """Download from Plex into the cache directory. Returns the cache file path or None."""
        cache_dir = self.cache_path or tempfile.gettempdir()
        try:
            os.makedirs(cache_dir, exist_ok=True)
        except Exception:
            pass

        # Unique temp path so parallel runs/crashes don't collide
        fd, cache_file = tempfile.mkstemp(prefix='plex-', suffix='-' + filename, dir=cache_dir)
        os.close(fd)
        cache_basename = os.path.basename(cache_file)
        cache_dirname = os.path.dirname(cache_file)

        try:
            full_url = self.server._baseurl + url
            result = plexapi.utils.download(full_url, self.token, filename=cache_basename, savepath=cache_dirname)
            if not result:
                self._safe_remove(cache_file)
                return None
            return cache_file
        except NotFound:
            print(f'\033[91mNOT FOUND (404):\033[0m Plex cannot find the asset file for {filename}.')
            self._safe_remove(cache_file)
            return None
        except Exception as e:
            print(f'\033[91mDOWNLOAD ERROR ({filename}):\033[0m {e}')
            self._safe_remove(cache_file)
            return None

    def _apply_local(self, cache_file, local_path, action, source_updated_at=None):
        """Place cache_file at local_path according to action. Returns True if a write occurred."""
        local_dir = os.path.dirname(local_path)
        file_existed = os.path.isfile(local_path)

        if not os.path.exists(local_dir):
            try:
                os.makedirs(local_dir)
            except Exception as e:
                print(f'\033[91mLOCAL MKDIR ERROR:\033[0m {e}')
                return False

        if action == 'mirror' and file_existed:
            if self._files_equal(cache_file, local_path):
                # Hash matched — no write needed. But realign mtime so the next run can
                # detect "untouched" via mtime alone and skip the hash entirely.
                self._align_mtime(local_path, source_updated_at)
                if self.verbose:
                    print('\033[93mLOCAL SKIPPED (Matches):\033[0m', local_path)
                return False

        try:
            # copy2 works across filesystems (cache on SSD → target on NAS) and preserves mtime
            shutil.copy2(cache_file, local_path)
            # Align local mtime with Plex's updatedAt so future runs can cheaply detect
            # external modification (local mtime > Plex updatedAt == something touched it).
            self._align_mtime(local_path, source_updated_at)
            if self.verbose:
                if action == 'mirror' and file_existed:
                    label = 'LOCAL REPLACED (Differs)'
                elif file_existed:
                    label = 'LOCAL OVERWRITTEN'
                else:
                    label = 'LOCAL CREATED'
                print(f'\033[92m{label}:\033[0m', local_path)
            return True
        except Exception as e:
            print(f'\033[91mLOCAL WRITE ERROR:\033[0m {e}')
            return False

    @staticmethod
    def _align_mtime(path, source_updated_at):
        if not source_updated_at:
            return
        try:
            epoch = source_updated_at.timestamp()
            os.utime(path, (epoch, epoch))
        except Exception:
            pass

    def _stage_for_rclone(self, cache_file, rel_path):
        """Copy cache_file into the staging tree at rel_path (rel_path includes filename).
        At end of run, finalize_rclone() will bulk-copy the entire staging tree."""
        if not self.rclone_staging or not rel_path:
            return False
        target = os.path.join(self.rclone_staging, rel_path)
        target_dir = os.path.dirname(target)
        try:
            if target_dir:
                os.makedirs(target_dir, exist_ok=True)
            shutil.copy2(cache_file, target)
            self.rclone_staged += 1
            if self.verbose:
                print('\033[92mRCLONE STAGED:\033[0m', rel_path)
            return True
        except Exception as e:
            print(f'\033[91mRCLONE STAGE ERROR:\033[0m {e}')
            return False

    def finalize_rclone(self):
        """One bulk `rclone copy --checksum` of the entire staging tree to the destination.
        Called once at end of main(). Resolves destination directories once (no race),
        skips files whose checksums match (Google Drive stores MD5)."""
        if not self.rclone_staging:
            return

        try:
            if self.rclone_staged == 0:
                if self.verbose:
                    print('\033[93mRCLONE FINAL:\033[0m nothing staged this run, skipping bulk copy.')
                return

            if self.dry_run:
                print(f'\033[96mDRY RUN:\033[0m Would bulk-copy {self.rclone_staged} staged file(s) → {self.rclone_dest}')
                return

            cmd = ['rclone']
            if self.rclone_config:
                cmd.extend(['--config', self.rclone_config])
            cmd.append('copy')
            if not self.force:
                cmd.append('--checksum')
            # Trailing slash on src means "contents of"
            src = self.rclone_staging.rstrip('/') + '/'
            cmd.extend([src, self.rclone_dest])

            if self.verbose:
                print(f'\033[94mRCLONE FINAL:\033[0m bulk-copying {self.rclone_staged} file(s) → {self.rclone_dest}')

            try:
                result = subprocess.run(cmd, capture_output=True, text=True)
                if result.returncode == 0:
                    print(f'\033[92mRCLONE BULK SYNCED:\033[0m {self.rclone_staged} file(s) → {self.rclone_dest}')
                    if self.verbose and result.stdout.strip():
                        print(result.stdout.strip())
                else:
                    print('\033[91mRCLONE BULK SYNC FAILED:\033[0m')
                    err = (result.stderr or '').strip()
                    if err:
                        print(err)
                    self.errors += 1
            except Exception as e:
                print(f'\033[91mRCLONE BULK SYNC ERROR:\033[0m {e}')
                self.errors += 1
        finally:
            # Always clean up staging, success or failure. Next run starts fresh.
            try:
                shutil.rmtree(self.rclone_staging, ignore_errors=True)
            except Exception:
                pass

    def download(self, url=None, filename=None, abs_dir=None, source_updated_at=None):
        if not url or not abs_dir:
            return

        # Guard against library root paths (less than 2 path segments deep)
        stripped = abs_dir.strip('/')
        if len(stripped.split('/')) < 2:
            print(f'\033[91mSKIPPED (Unsafe Path):\033[0m {abs_dir} looks like a library root — refusing to write here.')
            return

        # ---- compute target paths ----
        local_path = None
        if 'local' in self.export_types:
            if self.output_path == '/':
                local_dir = abs_dir
            else:
                local_dir = os.path.join(self.output_path, abs_dir.lstrip('/'))
            local_path = os.path.join(local_dir, filename)

        rclone_rel = None  # staging-relative path including filename
        if 'rclone' in self.export_types:
            rel_dir = self._relative_to_library(abs_dir)
            if rel_dir is None:
                print(f'\033[91mRCLONE WARN:\033[0m {abs_dir} is not under any library root; skipping rclone for this asset.')
            else:
                if rel_dir:
                    rclone_rel = rel_dir.strip('/') + '/' + filename
                else:
                    rclone_rel = filename

        # ---- decide what each target needs ----
        local_action = self._decide_local_action(local_path, source_updated_at) if local_path else 'none'

        # Bulk-rclone strategy: we only stage assets that were freshly downloaded for local
        # reasons. If local says "skip", we trust rclone destination is also current (we
        # pushed it last time we touched this file). This trades the self-healing-rclone
        # property for huge bandwidth savings. A periodic --force run can resync rclone
        # if drift is ever a concern. In rclone-only mode (no local), we have nothing to
        # gate on, so we always download.
        if local_path:
            need_fresh = local_action in ('download', 'mirror')
        else:
            need_fresh = rclone_rel is not None

        if not need_fresh:
            if self.verbose:
                print('\033[93mSKIPPED (Current):\033[0m', local_path or '(no targets)')
            self.skipped += 1
            return

        # ---- dry run ----
        if self.dry_run:
            actions = []
            if local_path and local_action in ('download', 'mirror'):
                actions.append(f'local {local_action} → {local_path}')
            if rclone_rel:
                actions.append(f'rclone stage → {rclone_rel}')
            print(f'\033[96mDRY RUN:\033[0m {" + ".join(actions) if actions else "(nothing)"}')
            self.downloaded += 1
            return

        # ---- single download, then local apply + rclone stage ----
        cache_file = self._download_to_cache(url, filename)
        if not cache_file:
            self.errors += 1
            return

        did_work = False
        try:
            if local_path and local_action in ('download', 'mirror'):
                if self._apply_local(cache_file, local_path, local_action, source_updated_at):
                    did_work = True
            if rclone_rel:
                if self._stage_for_rclone(cache_file, rclone_rel):
                    did_work = True
        finally:
            self._safe_remove(cache_file)

        if did_work:
            self.downloaded += 1
        else:
            self.skipped += 1


# main
@click.command(context_settings=dict(help_option_names=['-h', '--help']))
@click.version_option(prog_name=NAME, version=VERSION, message='%(prog)s v%(version)s')
@click.option('--baseurl', prompt='Plex Server URL', help='The base URL for the Plex server.', required=True)
@click.option('--token', prompt='Plex Token', help='The authentication token for Plex.', required=True)
@click.option('--library', help='The Plex library name.')
@click.option('--assets', help='Which assets should be exported?', type=click.Choice(['all', 'posters', 'backgrounds', 'banners', 'themes']), default='all')
@click.option('--output-path', default='/', help='The local output path root for downloaded assets.')
@click.option('--export-type', default='local',
              help='Comma-separated targets: local, rclone, or local,rclone. Default: local.')
@click.option('--rclone-dest', default=None,
              help='rclone destination prefix (e.g., gdrive:JellyfinPosters/TV). The library-relative path is appended. Required when --export-type includes rclone.')
@click.option('--rclone-config', default=None,
              help='Path to a custom rclone config file (passed as `rclone --config <path>`). Useful when the config lives in a bind-mounted location inside a container. If omitted, rclone uses its default lookup (~/.config/rclone/rclone.conf or $RCLONE_CONFIG).')
@click.option('--cache-path', default=None,
              help='Directory used for temporary downloads and hashing. Strongly recommended to point at a fast local SSD when your media lives on a NAS. Defaults to the system temp dir.')
@click.option('--force', help='Force overwrite of all assets regardless of timestamp or content.', is_flag=True)
@click.option('--mirror', help='Self-heal mode: download every asset, hash-compare with the local copy, and replace only if the bytes differ. Also re-downloads missing files.', is_flag=True)
@click.option('--force-hash', help='In mirror mode, hash-verify every existing file regardless of mtime. Useful as a periodic audit or after snapshot rollbacks / rsync restores where mtime may have been preserved. Implies --mirror. Slower (reads every file over NFS).', is_flag=True)
@click.option('--dry-run', help='Log what would be downloaded without writing any files.', is_flag=True)
@click.option('--verbose', help='Show extra information?', is_flag=True)
@click.pass_context
def main(ctx, baseurl: str, token: str, library: str, assets: str, force: bool, mirror: bool,
         verbose: bool, output_path: str, dry_run: bool, cache_path, export_type, rclone_dest,
         force_hash: bool, rclone_config):
    export_types = [t.strip().lower() for t in export_type.split(',') if t.strip()]

    plex = Plex(baseurl, token, library, force, verbose, output_path, dry_run, mirror,
                cache_path=cache_path, export_types=export_types, rclone_dest=rclone_dest,
                force_hash=force_hash, rclone_config=rclone_config)

    if dry_run:
        print('\033[96mDRY RUN MODE — no files will be written.\033[0m')

    if verbose:
        print('\033[94mASSETS:\033[0m', assets)
        print('\033[94mEXPORT TYPES:\033[0m', ', '.join(export_types))
        if 'rclone' in export_types:
            print('\033[94mRCLONE DEST:\033[0m', rclone_dest)
            if rclone_config:
                print('\033[94mRCLONE CONFIG:\033[0m', rclone_config)
        if cache_path:
            print('\033[94mCACHE PATH:\033[0m', cache_path)
        else:
            print('\033[94mCACHE PATH:\033[0m', tempfile.gettempdir(), '(system default)')
        print('\033[94mFORCE OVERWRITE:\033[0m', str(force))
        print('\033[94mMIRROR MODE:\033[0m', str(plex.mirror))
        print('\033[94mFORCE HASH:\033[0m', str(force_hash))
        print('\nGetting library items...')

    items = plex.getAll()

    use_progress = TQDM_AVAILABLE and not verbose
    iterator = tqdm(items, desc='Exporting', unit='item') if use_progress else items

    for item in iterator:
        # Reload to get freshest URLs and timestamps
        try:
            item.reload()
        except Exception:
            continue

        if verbose:
            print('\n\033[94mITEM:\033[0m', item.title)

        try:
            path = plex.getPath(item)
            if path is None:
                if verbose:
                    print(f'  [SKIP] No path found for {item.title}')
                continue

            item_updated = getattr(item, 'updatedAt', None)

            # MOVIE / SHOW LEVEL ASSETS
            if (assets == 'all' or assets == 'posters') and getattr(item, 'thumb', None):
                plex.download(item.thumb, 'poster.jpg', path, item_updated)

            if (assets == 'all' or assets == 'backgrounds') and getattr(item, 'art', None):
                plex.download(item.art, 'fanart.jpg', path, item_updated)

            if (assets == 'all' or assets == 'banners') and getattr(item, 'banner', None):
                plex.download(item.banner, 'banner.jpg', path, item_updated)

            if (assets == 'all' or assets == 'themes') and getattr(item, 'theme', None):
                plex.download(item.theme, 'theme.mp3', path, item_updated)

            # TV SPECIFIC
            if plex.library.type == 'show':
                for season in item.seasons():
                    season_path = plex.getPath(season)
                    season_updated = getattr(season, 'updatedAt', None)

                    # season.episodes() is fetched once and reused below for both the
                    # season-path fallback and the per-episode loop.
                    episodes = list(season.episodes())

                    # If plexapi didn't populate season.locations (it's unreliable
                    # depending on how the section was indexed), derive the season's
                    # directory from the first episode's media file path.
                    if not season_path and episodes:
                        for media in episodes[0].media:
                            for part in media.parts:
                                season_path = os.path.dirname(part.file)
                                break
                            if season_path:
                                break

                    # Season-level assets (only if we resolved a path)
                    if season_path:
                        if (assets == 'all' or assets == 'posters') and getattr(season, 'thumb', None):
                            plex.download(season.thumb, 'folder.jpg', season_path, season_updated)

                        if (assets == 'all' or assets == 'backgrounds') and getattr(season, 'art', None):
                            plex.download(season.art, 'season-fanart.jpg', season_path, season_updated)

                        if (assets == 'all' or assets == 'banners') and getattr(season, 'banner', None):
                            plex.download(season.banner, 'season-banner.jpg', season_path, season_updated)
                    elif verbose:
                        print(f'  [SEASON SKIP] could not resolve path for {item.title} S{getattr(season, "seasonNumber", "?"):02d}')

                    # Episode-level processing is INDEPENDENT of season_path —
                    # each episode resolves its own directory from its media file.
                    for episode in episodes:
                        episode_updated = getattr(episode, 'updatedAt', None)

                        if (assets == 'all' or assets == 'posters') and getattr(episode, 'thumb', None):
                            ep_path = None
                            ep_filename = None
                            for media in episode.media:
                                for part in media.parts:
                                    ep_path = os.path.dirname(part.file)
                                    ep_filename = os.path.splitext(os.path.basename(part.file))[0] + '-thumb.jpg'
                                    break
                                if ep_path:
                                    break
                            if ep_path and ep_filename:
                                plex.download(episode.thumb, ep_filename, ep_path, episode_updated)

        except Exception as e:
            print(f'\033[91mERROR Processing {item.title}:\033[0m {e}')

    # End of per-asset loop — do the single bulk rclone copy now (no-op if nothing staged)
    print()
    plex.finalize_rclone()

    print()
    if dry_run:
        print('\033[96mDRY RUN COMPLETE\033[0m')
        print('\033[94mWOULD PROCESS:\033[0m', str(plex.downloaded))
    else:
        print('\033[94mTOTAL SKIPPED:\033[0m', str(plex.skipped))
        print('\033[94mTOTAL PROCESSED:\033[0m', str(plex.downloaded))
        if plex.rclone_staging is not None:
            print('\033[94mRCLONE STAGED:\033[0m', str(plex.rclone_staged))
        if plex.errors:
            print('\033[91mTOTAL ERRORS:\033[0m', str(plex.errors))


# run
if __name__ == '__main__':
    main(obj={})
