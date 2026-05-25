#!/usr/bin/env python3

import os
import sys
import datetime
import hashlib
import shutil
import subprocess
import tempfile
import json
import urllib.request
import urllib.error

# pwd/grp are POSIX-only; we use them for --owner name resolution
try:
    import pwd
    import grp
    _HAVE_USER_LOOKUP = True
except ImportError:
    _HAVE_USER_LOOKUP = False

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
VERSION = '0.20'

VALID_EXPORT_TYPES = {'local', 'rclone'}


# plex
class Plex():
    def __init__(self, baseurl=None, token=None, library=None, force=False, verbose=False,
                 output_path=None, dry_run=False, mirror=False, cache_path=None,
                 export_types=None, rclone_dest=None, force_hash=False, rclone_config=None,
                 owner=None, jellyfin_url=None, jellyfin_api_key=None, jellyfin_task=None):
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
        self.owner_uid = None
        self.owner_gid = None
        if owner:
            self._parse_owner(owner)
        self.jellyfin_url = (jellyfin_url or '').rstrip('/') or None
        self.jellyfin_api_key = jellyfin_api_key or None
        self.jellyfin_task = jellyfin_task or None
        self.unmatched_year = []  # titles without year metadata — won't match LP Primary regexes
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

        # Validate local output path — must be set to a real directory (not / or empty).
        # Local writes now use Local Posters naming under this directory.
        if 'local' in self.export_types:
            if not self.output_path or self.output_path == '/':
                print('\033[91mERROR:\033[0m --output-path must be set to a real posters directory '
                      'when --export-type includes local (e.g. /data/media-posters). '
                      'The script writes Local Posters-formatted files under this root.')
                sys.exit()
            try:
                os.makedirs(self.output_path, exist_ok=True)
            except Exception as e:
                print(f'\033[91mERROR:\033[0m could not create output path {self.output_path}: {e}')
                sys.exit()

        # Validate rclone availability if needed
        if 'rclone' in self.export_types:
            if not shutil.which('rclone'):
                print('\033[91mERROR:\033[0m rclone is not installed or not in PATH.')
                sys.exit()
            if not self.rclone_dest:
                print('\033[91mERROR:\033[0m --rclone-dest is required when --export-type includes rclone.')
                sys.exit()

        # Validate jellyfin webhook — all three params required together
        any_jf = any([self.jellyfin_url, self.jellyfin_api_key, self.jellyfin_task])
        all_jf = all([self.jellyfin_url, self.jellyfin_api_key, self.jellyfin_task])
        if any_jf and not all_jf:
            print('\033[91mERROR:\033[0m --jellyfin-url, --jellyfin-api-key, and --jellyfin-task '
                  'must all be set together (or none).')
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

    def _parse_owner(self, spec):
        """Parse '--owner' spec into self.owner_uid / self.owner_gid.
        Accepted forms: 'uid', 'uid:gid', 'user', 'user:group', 'user:gid', 'uid:group'.
        If group is omitted, the user's primary group is used (when name resolution works);
        otherwise the gid defaults to the uid value."""
        parts = spec.split(':', 1)
        user_part = parts[0].strip()
        group_part = parts[1].strip() if len(parts) > 1 else None

        def resolve_user(token):
            try:
                return int(token)
            except ValueError:
                if not _HAVE_USER_LOOKUP:
                    print(f'\033[91mERROR:\033[0m --owner needs numeric uid on this platform (no pwd module).')
                    sys.exit()
                try:
                    return pwd.getpwnam(token).pw_uid
                except KeyError:
                    print(f'\033[91mERROR:\033[0m unknown user "{token}" in --owner.')
                    sys.exit()

        def resolve_group(token):
            try:
                return int(token)
            except ValueError:
                if not _HAVE_USER_LOOKUP:
                    print(f'\033[91mERROR:\033[0m --owner needs numeric gid on this platform (no grp module).')
                    sys.exit()
                try:
                    return grp.getgrnam(token).gr_gid
                except KeyError:
                    print(f'\033[91mERROR:\033[0m unknown group "{token}" in --owner.')
                    sys.exit()

        self.owner_uid = resolve_user(user_part)
        if group_part is not None:
            self.owner_gid = resolve_group(group_part)
        elif _HAVE_USER_LOOKUP:
            try:
                self.owner_gid = pwd.getpwuid(self.owner_uid).pw_gid
            except KeyError:
                self.owner_gid = self.owner_uid
        else:
            self.owner_gid = self.owner_uid

    def _apply_owner(self, path):
        """chown path to the configured --owner, silently no-op if --owner wasn't set."""
        if self.owner_uid is None:
            return
        try:
            os.chown(path, self.owner_uid, self.owner_gid)
        except Exception as e:
            if self.verbose:
                print(f'  [CHOWN WARN] {path}: {e}')

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

    # ----- Local Posters filename builders -----
    # The Jellyfin Local Posters plugin (NooNameR/Jellyfin.Plugin.LocalPosters) uses
    # MediUX/TPDb naming. We generate filenames that match its regexes so the bulk
    # rclone-uploaded tree is directly consumable by the plugin's GDrive sync.
    #
    # Key constraints (from the plugin's C# matchers):
    #   - Brackets [] in filenames break the regex. Only {tag} braces tolerated.
    #   - Series, Season, and Movie Primary REQUIRE a 4-digit year.
    #   - Season and Episode item types only support Primary image (no per-season art).
    #   - Art types (Backdrop/Banner/Logo/Disc/Thumb/Art) follow: "Title (Year) - <Type>.ext".

    _LP_FILENAME_STRIP = '[]{}<>:"/\\|?*'

    @classmethod
    def _lp_clean(cls, title):
        if not title:
            return ''
        out = ''.join(' ' if c in cls._LP_FILENAME_STRIP else c for c in title)
        return ' '.join(out.split())  # collapse whitespace

    @classmethod
    def _lp_show_folder(cls, show_title, year):
        """Subfolder name under the rclone destination for one show's assets."""
        t = cls._lp_clean(show_title)
        return f'{t} ({year})' if year else t

    @classmethod
    def _lp_series_filename(cls, show_title, year, ext='jpg', art_type=None):
        """Primary (art_type=None) or Art (Backdrop/Banner/etc) for a Series."""
        t = cls._lp_clean(show_title)
        base = f'{t} ({year})' if year else t
        return f'{base} - {art_type}.{ext}' if art_type else f'{base}.{ext}'

    @classmethod
    def _lp_season_filename(cls, show_title, year, season_index, season_name=None, ext='jpg'):
        """Season Primary. Uses 'Season NN' for numbered seasons, season name for specials/named."""
        t = cls._lp_clean(show_title)
        if season_index and season_index > 0:
            tail = f'Season {season_index:02d}'
        else:
            tail = cls._lp_clean(season_name) or 'Specials'
        return f'{t} ({year}) - {tail}.{ext}'

    @classmethod
    def _lp_episode_filename(cls, show_title, year, season_index, episode_index, ext='jpg'):
        """Episode Primary."""
        t = cls._lp_clean(show_title)
        base = f'{t} ({year})' if year else t
        return f'{base} - S{season_index:02d}E{episode_index:02d}.{ext}'

    @classmethod
    def _lp_movie_filename(cls, movie_title, year, ext='jpg', art_type=None):
        """Primary (art_type=None) or Art for a Movie."""
        t = cls._lp_clean(movie_title)
        base = f'{t} ({year})' if year else t
        return f'{base} - {art_type}.{ext}' if art_type else f'{base}.{ext}'

    @classmethod
    def _lp_collection_filename(cls, collection_title, ext='jpg', art_type=None):
        """BoxSet Primary or Art. The plugin's MovieCollection regex requires the
        filename (minus art-type suffix) to end with the literal word 'Collection'.
        If the Plex collection title already ends in 'Collection' we don't double it up."""
        t = cls._lp_clean(collection_title).rstrip()
        if not t.lower().endswith(' collection') and not t.lower().endswith('collection'):
            t = f'{t} Collection'
        elif not t.lower().endswith(' collection'):
            # Title was "...Collection" with no preceding space — normalize to "... Collection"
            t = t[:-len('Collection')].rstrip() + ' Collection'
        return f'{t} - {art_type}.{ext}' if art_type else f'{t}.{ext}'

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
                self._apply_owner(local_dir)
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
            # Cache file inherits 0600 from tempfile.mkstemp and copy2 preserves it, so
            # without this chmod every asset is unreadable to any user other than the
            # one running the script. Media servers (Jellyfin, Plex, etc.) typically run
            # as their own user, not root.
            try:
                os.chmod(local_path, 0o644)
            except Exception:
                pass
            self._apply_owner(local_path)
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

    def trigger_jellyfin_task(self):
        """Optionally POST to Jellyfin's ScheduledTasks API to run a named task
        (typically 'Match and Update local posters' for the Local Posters plugin)
        after a successful run. No-op if --jellyfin-* args weren't provided."""
        if not (self.jellyfin_url and self.jellyfin_api_key and self.jellyfin_task):
            return

        if self.dry_run:
            print(f'\033[96mDRY RUN:\033[0m Would trigger Jellyfin task matching "{self.jellyfin_task}"')
            return

        headers = {
            'X-Emby-Token': self.jellyfin_api_key,
            'Accept': 'application/json',
        }

        # 1) List tasks, find matching one by case-insensitive substring on Name
        list_url = f'{self.jellyfin_url}/ScheduledTasks'
        try:
            req = urllib.request.Request(list_url, headers=headers)
            with urllib.request.urlopen(req, timeout=30) as resp:
                tasks = json.loads(resp.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            print(f'\033[91mJELLYFIN:\033[0m list tasks failed: HTTP {e.code} {e.reason}')
            self.errors += 1
            return
        except Exception as e:
            print(f'\033[91mJELLYFIN:\033[0m list tasks failed: {e}')
            self.errors += 1
            return

        needle = self.jellyfin_task.lower()
        matching = [t for t in tasks if needle in (t.get('Name') or '').lower()]

        if not matching:
            print(f'\033[91mJELLYFIN:\033[0m no task matching "{self.jellyfin_task}". '
                  f'Available task names:')
            for t in tasks:
                print(f'  - {t.get("Name")}')
            self.errors += 1
            return

        if len(matching) > 1:
            print(f'\033[91mJELLYFIN:\033[0m multiple tasks match "{self.jellyfin_task}":')
            for t in matching:
                print(f'  - {t.get("Name")}')
            print('  Use a more specific name.')
            self.errors += 1
            return

        task = matching[0]
        task_id = task.get('Id')
        task_name = task.get('Name')

        # 2) POST to trigger it
        run_url = f'{self.jellyfin_url}/ScheduledTasks/Running/{task_id}'
        try:
            req = urllib.request.Request(run_url, headers=headers, method='POST', data=b'')
            with urllib.request.urlopen(req, timeout=30) as resp:
                # 204 No Content is the success response
                print(f'\033[92mJELLYFIN:\033[0m triggered task "{task_name}" (HTTP {resp.status})')
        except urllib.error.HTTPError as e:
            print(f'\033[91mJELLYFIN:\033[0m trigger failed: HTTP {e.code} {e.reason}')
            self.errors += 1
        except Exception as e:
            print(f'\033[91mJELLYFIN:\033[0m trigger failed: {e}')
            self.errors += 1


    def download(self, url=None, rel_path=None, source_updated_at=None):
        """Download a Plex asset and route it to all configured destinations.

        `rel_path` is the Local Posters-style relative path including filename, e.g.
        'Gold Rush (2010)/Gold Rush (2010) - S16E01.jpg'. Both local and rclone
        destinations use this same relative path under their respective roots:
          local:  <output_path>/<rel_path>
          rclone: <rclone_dest>/<rel_path>
        """
        if not url or not rel_path:
            return

        rel_path = rel_path.lstrip('/')

        # Compute local target
        local_path = None
        if 'local' in self.export_types:
            local_path = os.path.join(self.output_path, rel_path)

        # Compute rclone target (uses same rel_path under rclone_dest)
        rclone_rel = rel_path if 'rclone' in self.export_types else None

        # Nothing to do at all?
        if not local_path and not rclone_rel:
            return

        # ---- decide what each target needs ----
        local_action = self._decide_local_action(local_path, source_updated_at) if local_path else 'none'

        needs_local_apply = bool(local_path) and local_action in ('download', 'mirror')
        needs_rclone = rclone_rel is not None

        # Source decision (same as before):
        #   - Local needs fresh bytes → download from Plex, that cache feeds rclone too
        #   - Local current but rclone needs file → use existing local file as source
        #   - rclone-only with no local file → download from Plex
        can_use_local_as_source = (
            needs_rclone
            and not needs_local_apply
            and local_path
            and os.path.isfile(local_path)
        )
        need_plex_download = needs_local_apply or (needs_rclone and not can_use_local_as_source)

        if not needs_local_apply and not needs_rclone:
            if self.verbose:
                print('\033[93mSKIPPED (Current):\033[0m', local_path or rclone_rel or '(no targets)')
            self.skipped += 1
            return

        # ---- dry run ----
        if self.dry_run:
            actions = []
            if needs_local_apply:
                actions.append(f'local {local_action} → {local_path}')
            if needs_rclone:
                src_label = 'from Plex' if need_plex_download else 'from local'
                actions.append(f'rclone stage {src_label} → {rclone_rel}')
            print(f'\033[96mDRY RUN:\033[0m {" + ".join(actions) if actions else "(nothing)"}')
            self.downloaded += 1
            return

        # ---- acquire source bytes ----
        cache_file = None
        if need_plex_download:
            cache_label = os.path.basename(rel_path)
            cache_file = self._download_to_cache(url, cache_label)
            if not cache_file:
                self.errors += 1
                return
            source_file = cache_file
        else:
            source_file = local_path

        did_work = False
        try:
            if needs_local_apply:
                if self._apply_local(source_file, local_path, local_action, source_updated_at):
                    did_work = True
            if needs_rclone:
                if self._stage_for_rclone(source_file, rclone_rel):
                    did_work = True
        finally:
            if cache_file:
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
@click.option('--output-path', default=None,
              help='Local Posters root directory for the local export. Files are written here in '
                   'Local Posters / MediUX / TPDb naming convention (e.g. '
                   '"Gold Rush (2010)/Gold Rush (2010) - S01E01.jpg"). Required when --export-type '
                   'includes local. Point your Local Posters plugin at this directory.')
@click.option('--export-type', default='local',
              help='Comma-separated targets: local, rclone, or local,rclone. Default: local.')
@click.option('--rclone-dest', default=None,
              help='rclone destination prefix (e.g., gdrive:JellyfinPosters/TV). The library-relative path is appended. Required when --export-type includes rclone.')
@click.option('--rclone-config', default=None,
              help='Path to a custom rclone config file (passed as `rclone --config <path>`). Useful when the config lives in a bind-mounted location inside a container. If omitted, rclone uses its default lookup (~/.config/rclone/rclone.conf or $RCLONE_CONFIG).')
@click.option('--cache-path', default=None,
              help='Directory used for temporary downloads and hashing. Strongly recommended to point at a fast local SSD when your media lives on a NAS. Defaults to the system temp dir.')
@click.option('--owner', default=None,
              help='Optional uid:gid (or user:group, or just user/uid) to chown created files and directories to. Useful when the script runs as root inside a container but your media server runs as a different user (e.g. "downloader:downloaders" or "1000:1000"). Numeric IDs are always accepted; names require the user/group to exist inside the script\'s container.')
@click.option('--jellyfin-url', default=None,
              help='Optional Jellyfin base URL (e.g. http://jellyfin:8096). When set with '
                   '--jellyfin-api-key and --jellyfin-task, the matching Jellyfin scheduled task '
                   'is triggered via API after the run completes.')
@click.option('--jellyfin-api-key', default=None,
              help='Jellyfin API key (Dashboard → API Keys). Required if --jellyfin-url is set.')
@click.option('--jellyfin-task', default=None,
              help='Case-insensitive substring of the Jellyfin scheduled task name to trigger. '
                   'For the Local Posters plugin, "Match and Update local posters" makes new files '
                   'become visible artwork.')
@click.option('--force', help='Force overwrite of all assets regardless of timestamp or content.', is_flag=True)
@click.option('--mirror', help='Self-heal mode: download every asset, hash-compare with the local copy, and replace only if the bytes differ. Also re-downloads missing files.', is_flag=True)
@click.option('--force-hash', help='In mirror mode, hash-verify every existing file regardless of mtime. Useful as a periodic audit or after snapshot rollbacks / rsync restores where mtime may have been preserved. Implies --mirror. Slower (reads every file over NFS).', is_flag=True)
@click.option('--dry-run', help='Log what would be downloaded without writing any files.', is_flag=True)
@click.option('--verbose', help='Show extra information?', is_flag=True)
@click.pass_context
def main(ctx, baseurl: str, token: str, library: str, assets: str, force: bool, mirror: bool,
         verbose: bool, output_path: str, dry_run: bool, cache_path, export_type, rclone_dest,
         force_hash: bool, rclone_config, owner, jellyfin_url, jellyfin_api_key, jellyfin_task):
    export_types = [t.strip().lower() for t in export_type.split(',') if t.strip()]

    plex = Plex(baseurl, token, library, force, verbose, output_path, dry_run, mirror,
                cache_path=cache_path, export_types=export_types, rclone_dest=rclone_dest,
                force_hash=force_hash, rclone_config=rclone_config, owner=owner,
                jellyfin_url=jellyfin_url, jellyfin_api_key=jellyfin_api_key,
                jellyfin_task=jellyfin_task)

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
        if owner:
            print('\033[94mOWNER:\033[0m', f'{plex.owner_uid}:{plex.owner_gid}')
        if plex.jellyfin_url:
            print('\033[94mJELLYFIN URL:\033[0m', plex.jellyfin_url)
            print('\033[94mJELLYFIN TASK:\033[0m', plex.jellyfin_task)
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
            is_show_library = (plex.library.type == 'show')

            # Local Posters keys: show/movie title + production year. Year is required for
            # Series/Season/Movie Primary matches (regex demands \(\d{4}\)).
            # Per-item warning is intentionally suppressed; missing-year titles are
            # accumulated and reported in the end-of-run summary.
            item_title = getattr(item, 'title', None)
            item_year = getattr(item, 'year', None)
            if not item_year and item_title:
                plex.unmatched_year.append(item_title)
            show_folder = plex._lp_show_folder(item_title, item_year)

            # MOVIE / SHOW LEVEL ASSETS
            if (assets == 'all' or assets == 'posters') and getattr(item, 'thumb', None):
                if is_show_library:
                    rel = f'{show_folder}/{plex._lp_series_filename(item_title, item_year)}'
                else:
                    rel = plex._lp_movie_filename(item_title, item_year)
                plex.download(item.thumb, rel, item_updated)

            if (assets == 'all' or assets == 'backgrounds') and getattr(item, 'art', None):
                if is_show_library:
                    rel = f'{show_folder}/{plex._lp_series_filename(item_title, item_year, art_type="Backdrop")}'
                else:
                    rel = plex._lp_movie_filename(item_title, item_year, art_type='Backdrop')
                plex.download(item.art, rel, item_updated)

            if (assets == 'all' or assets == 'banners') and getattr(item, 'banner', None):
                if is_show_library:
                    rel = f'{show_folder}/{plex._lp_series_filename(item_title, item_year, art_type="Banner")}'
                else:
                    rel = plex._lp_movie_filename(item_title, item_year, art_type='Banner')
                plex.download(item.banner, rel, item_updated)

            # theme.mp3 is not handled by Local Posters; not emitted.

            # TV SPECIFIC
            if is_show_library:
                for season in item.seasons():
                    season_updated = getattr(season, 'updatedAt', None)
                    season_idx = getattr(season, 'index', None) or getattr(season, 'seasonNumber', None)
                    season_name = getattr(season, 'title', None)

                    # Season Primary
                    if (assets == 'all' or assets == 'posters') and getattr(season, 'thumb', None):
                        rel = (f'{show_folder}/'
                               f'{plex._lp_season_filename(item_title, item_year, season_idx, season_name)}')
                        plex.download(season.thumb, rel, season_updated)

                    # Local Posters doesn't support season Backdrop/Banner — not emitted.

                    # Episode Primary
                    for episode in season.episodes():
                        episode_updated = getattr(episode, 'updatedAt', None)
                        ep_idx = getattr(episode, 'index', None)

                        if (assets == 'all' or assets == 'posters') and getattr(episode, 'thumb', None):
                            if (season_idx is not None) and (ep_idx is not None):
                                rel = (f'{show_folder}/'
                                       f'{plex._lp_episode_filename(item_title, item_year, season_idx, ep_idx)}')
                                plex.download(episode.thumb, rel, episode_updated)

        except Exception as e:
            print(f'\033[91mERROR Processing {item.title}:\033[0m {e}')

    # ---- Plex Collections (Local Posters BoxSets) ----
    # Collections are virtual groupings in Plex — no on-disk folder. We only emit
    # rclone artwork for them (under a "Collections/" subfolder of the rclone dest).
    # Skip entirely if rclone isn't configured.
    if 'rclone' in plex.export_types:
        try:
            collections = list(plex.library.collections())
        except Exception as e:
            collections = []
            if verbose:
                print(f'\n\033[93m[COLLECTIONS] could not enumerate:\033[0m {e}')

        for col in collections:
            try:
                col.reload()
            except Exception:
                continue

            col_title = getattr(col, 'title', None)
            if not col_title:
                continue

            if verbose:
                print(f'\n\033[94mCOLLECTION:\033[0m {col_title}')

            col_updated = getattr(col, 'updatedAt', None)

            try:
                if (assets == 'all' or assets == 'posters') and getattr(col, 'thumb', None):
                    rel = 'Collections/' + plex._lp_collection_filename(col_title)
                    plex.download(col.thumb, rel, col_updated)

                if (assets == 'all' or assets == 'backgrounds') and getattr(col, 'art', None):
                    rel = 'Collections/' + plex._lp_collection_filename(col_title, art_type='Backdrop')
                    plex.download(col.art, rel, col_updated)
            except Exception as e:
                print(f'\033[91mERROR Processing collection {col_title}:\033[0m {e}')

    # End of per-asset loop — do the single bulk rclone copy now (no-op if nothing staged)
    print()
    plex.finalize_rclone()

    # Trigger Jellyfin scheduled task (no-op if --jellyfin-* not set)
    plex.trigger_jellyfin_task()

    print()
    if dry_run:
        print('\033[96mDRY RUN COMPLETE\033[0m')
        print('\033[94mWOULD PROCESS:\033[0m', str(plex.downloaded))
    else:
        print('\033[94mTOTAL SKIPPED:\033[0m', str(plex.skipped))
        print('\033[94mTOTAL PROCESSED:\033[0m', str(plex.downloaded))
        if plex.rclone_staging is not None:
            print('\033[94mRCLONE STAGED:\033[0m', str(plex.rclone_staged))
        if plex.unmatched_year:
            print(f'\033[93mNO YEAR ({len(plex.unmatched_year)}):\033[0m '
                  f'Primary images for these won\'t match Local Posters regex (Art types still work):')
            for t in sorted(plex.unmatched_year):
                print(f'  - {t}')
        if plex.errors:
            print('\033[91mTOTAL ERRORS:\033[0m', str(plex.errors))


# run
if __name__ == '__main__':
    # Default umask of 0o077 (some containers) results in directories created at 700,
    # which a non-root media server can't traverse. Force a sane umask up front so any
    # directories the script creates are 755 by default.
    os.umask(0o022)
    main(obj={})
