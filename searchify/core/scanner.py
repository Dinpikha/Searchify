"""Registered-root scanning with per-directory rules and transient hashing."""
from dataclasses import asdict, dataclass, field
import fnmatch
import hashlib
import os
import re
from pathlib import Path

from searchify.core.extractor import TEXT_EXTENSIONS, MAX_CONTENT_SIZE, extract_content

PROTECTED_DIRS = {'.git','.aws','.ssh','.gnupg','.kube','.secrets','.searchify'}

PROTECTED = ('.env', '.env.*', '*.jks', '*.keystore', '*.pem', '*.key',
             '*credentials*', '*service-account*.json', 'google-services.json', 'local.properties')


@dataclass
class DirectoryRules:
    include: list = field(default_factory=lambda: ['*'])
    exclude: list = field(default_factory=lambda: ['.git/**','**/node_modules/**','**/__pycache__/**','**/.venv/**','**/venv/**','**/dist/**','**/.next/**'])
    extensions: list = field(default_factory=lambda: sorted(TEXT_EXTENSIONS))
    max_size: int = MAX_CONTENT_SIZE
    sync_enabled: bool = True
    profile: str = 'source'

    @classmethod
    def from_dict(cls, value):
        if not isinstance(value,dict) or set(value)-{'include','exclude','extensions','max_size','sync_enabled','profile'}:
            raise ValueError('Invalid directory rule fields.')
        rules = cls(**value)
        if (type(rules.max_size) is not int or not 1 <= rules.max_size <= MAX_CONTENT_SIZE
                or type(rules.sync_enabled) is not bool):
            raise ValueError('Invalid directory size or synchronization setting.')
        for patterns in (rules.include, rules.exclude, rules.extensions):
            if not isinstance(patterns, list) or any(not isinstance(p, str) or not p or len(p)>256 for p in patterns):
                raise ValueError('Invalid directory patterns.')
        if any(extension not in TEXT_EXTENSIONS for extension in rules.extensions):
            raise ValueError('Extraction is not implemented for a requested extension.')
        return rules

    def to_dict(self):
        return asdict(self)

    @classmethod
    def preset(cls, profile):
        if profile == 'source':
            return cls()
        if profile == 'android':
            return cls(profile='android', exclude=['.git/**', '**/build/**', '**/.gradle/**', '**/.kotlin/**',
                       '**/.idea/**', '**/.cxx/**', '**/.externalNativeBuild/**', '**/generated/**',
                       '**/debug/output-metadata.json', '**/captures/**', '*.log'],
                       extensions=['.kt', '.kts', '.java', '.xml', '.json', '.gradle', '.properties', '.toml',
                                   '.pro', '.md', '.txt', '.yaml', '.yml', '.sh', '.c', '.cpp', '.h'])
        if profile == 'documents':
            return cls(profile='documents', extensions=['.txt', '.md', '.csv', '.rst'],
                       exclude=['.git/**', '~*', '*.tmp', '*.bak'])
        raise ValueError('Unknown directory profile.')


def matches(path, patterns):
    path = path.replace(os.sep, '/')
    for pattern in patterns:
        variants = [pattern]
        while variants[-1].startswith('**/'):
            variants.append(variants[-1][3:])
        for variant in variants:
            if (fnmatch.fnmatchcase(path, variant) or Path(path).match(variant)
                    or (variant.endswith('/**') and matches(path, [variant[:-3]]))):
                return True
    return False


def signature(stat):
    return ':'.join(str(value) for value in (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns))


def root_is_unredirected(root):
    """Registered roots are canonical paths; never follow a replacement link.

    Checking the resolved path also catches a replaced ancestor, even when the
    root itself is not a symlink. Do not normalize away links before checking.
    """
    root = Path(root).absolute()
    try:
        return not root.is_symlink() and root.resolve() == root
    except (OSError, RuntimeError):
        return False


class Scanner:
    def __init__(self, registry, exclude_paths=()):
        self.registry = registry
        from searchify.config.settings import settings
        self.exclude_paths={Path(path).absolute() for path in (*exclude_paths,settings.REPORTS_DIR,settings.REVIEWS_DIR,settings.PLANS_DIR)}
        self.exclude_paths.update({registry.path,Path(str(registry.path)+'-wal'),Path(str(registry.path)+'-shm'),Path(str(registry.path)+'.sync.lock')})

    def scan(self, hash_contents=True):
        old, states = self.registry.metadata(), self.registry.states()
        entries, excluded = [], []
        directories=sorted(self.registry.directories(),key=lambda d:len(Path(d['path']).parts),reverse=True)
        registered_roots={Path(d['path']) for d in directories}
        for directory in directories:
            root = Path(directory['path'])
            if not root_is_unredirected(root):
                # Treat an unverified boundary as an incomplete scan, not as
                # deletion of every previously indexed file under that root.
                entries.append(dict(root_id=directory['id'], root_path=str(root), path=str(root),
                                    relative_path='.', filename=root.name, synthetic=True,
                                    change='unavailable', eligible=False, reason='incomplete_scan',
                                    root_error='registered_root_boundary_unverified'))
                continue
            rules = self.registry.rules(directory['id'])
            seen, complete = set(), True
            if any(part in PROTECTED_DIRS for part in root.parts):
                entries.append(dict(root_id=directory['id'],root_path=str(root),path=str(root),relative_path='.',filename=root.name,synthetic=True,change='unchanged',eligible=False,reason='protected_directory'))
                continue
            if root.absolute() in self.exclude_paths:
                entries.append(dict(root_id=directory['id'],root_path=str(root),path=str(root),relative_path='.',filename=root.name,synthetic=True,change='unchanged',eligible=False,reason='internal_metadata'))
                continue
            if not root.is_dir() or not rules.sync_enabled:
                entries.append(dict(root_id=directory['id'], root_path=str(root), path=str(root), relative_path='.',
                                    filename=root.name, synthetic=True, change='unavailable', eligible=False,
                                    reason='missing_directory' if rules.sync_enabled else 'sync_disabled'))
                continue
            def walk_error(error):
                nonlocal complete
                complete = False
            for current, dirs, names in os.walk(root, followlinks=False, onerror=walk_error):
                for name in sorted(dirs.copy()):
                    path = Path(current)/name
                    relative = path.relative_to(root).as_posix()
                    if path.absolute() in self.exclude_paths:
                        dirs.remove(name); excluded.append(str(path)); continue
                    if path in registered_roots and path!=root:
                        dirs.remove(name)
                        continue
                    if path.is_symlink() or name in PROTECTED_DIRS or matches(relative, rules.exclude):
                        dirs.remove(name)
                        excluded.append(str(path))
                dirs.sort()
                for name in sorted(names):
                    path = Path(current)/name
                    relative = path.relative_to(root).as_posix()
                    absolute = str(path.absolute())
                    seen.add(absolute)
                    previous = old.get(absolute)
                    state = states.get(absolute, {})
                    entry = dict(path=absolute, root_id=directory['id'], root_path=str(root), relative_path=relative,
                                 filename=name, extension=path.suffix.lower(), eligible=False, reason=None,
                                 size_bytes=0, modified_ns=0, signature=None, content_hash=None,
                                 max_size=rules.max_size, allowed_extensions=rules.extensions)
                    try:
                        stat = path.lstat()
                        entry.update(size_bytes=stat.st_size, modified_ns=stat.st_mtime_ns, signature=signature(stat))
                        entry['change'] = ('new' if previous is None else 'unchanged' if entry['signature']==state.get('signature') else
                                           'modified' if state.get('signature') or previous.get('size')!=stat.st_size or previous.get('mtime_ns')!=stat.st_mtime_ns else 'unverified')
                        if path.absolute() in self.exclude_paths:
                            entry['reason']='internal_metadata'
                        elif path.is_symlink():
                            entry['reason'] = 'symlink'
                        elif not path.is_file():
                            entry['reason'] = 'not_regular_file'
                        elif matches(name, PROTECTED):
                            entry['reason'] = 'protected_file'
                        elif matches(relative, rules.exclude) or not matches(relative, rules.include):
                            entry['reason'] = 'excluded_pattern'
                        elif entry['extension'] not in rules.extensions:
                            entry['reason'] = 'unsupported_file_type'
                        elif stat.st_size > rules.max_size:
                            entry['reason'] = 'size_restriction'
                        elif entry['change'] == 'unchanged' and state.get('reason') == 'empty_or_binary':
                            entry['reason'] = 'empty_or_binary'
                        elif hash_contents:
                            cached = state.get('content_hash')
                            if (entry['change'] == 'unchanged' and isinstance(cached, str) and re.fullmatch(r'[0-9a-f]{64}',cached)
                                    and cached != hashlib.sha256(b'').hexdigest()):
                                entry['content_hash'] = cached
                            else:
                                content = extract_content(path, rules.max_size, rules.extensions, strict=True)
                                after = path.stat()
                                if signature(after) != entry['signature']:
                                    entry['reason'] = 'changed_during_scan'
                                elif not content.strip():
                                    entry['reason'] = 'empty_or_binary'
                                else:
                                    entry['content_hash'] = hashlib.sha256(content.encode()).hexdigest()
                                del content
                            entry['eligible'] = entry['content_hash'] is not None
                        else:
                            entry['eligible'] = True
                            entry['content_hash'] = state.get('content_hash') if entry['change']=='unchanged' else None
                    except OSError as error:
                        from searchify.reporting.sync_report import describe_error
                        entry.update(change='unavailable', reason='local_read_error', scan_error=describe_error('extraction',error))
                    entries.append(entry)
            if complete:
                for path, previous in old.items():
                    if previous['root_id'] == directory['id'] and path not in seen:
                        # Nested registered roots own their own paths and rules.
                        if any(other!=root and other.is_relative_to(root) and Path(path).is_relative_to(other) for other in registered_roots):
                            continue
                        # Excluded subtrees are not missing files.
                        rel = Path(path).relative_to(root).as_posix()
                        if any(Path(path).is_relative_to(Path(folder)) for folder in excluded):
                            entries.append(dict(path=path,root_id=directory['id'],root_path=str(root),relative_path=rel,
                                filename=previous.get('name',Path(path).name),extension=previous.get('extension',Path(path).suffix.lower()),
                                size_bytes=previous.get('size',0),modified_ns=previous.get('mtime_ns',0),
                                signature=states.get(path,{}).get('signature'),content_hash=previous.get('sha256'),
                                change='unchanged',observed=False,eligible=False,reason='excluded_pattern'))
                            continue
                        entries.append(dict(path=path, root_id=directory['id'], root_path=str(root), relative_path=rel,
                                            filename=previous.get('name',Path(path).name), change='deleted', eligible=False, reason='missing_file'))
            else:
                entries.append(dict(path=str(root), root_id=directory['id'], root_path=str(root), relative_path='.',
                                    filename=root.name, synthetic=True, change='unavailable', eligible=False, reason='incomplete_scan'))
        metrics = {'total_discovered': sum(e['change'] not in {'deleted','unavailable'} and e.get('observed',True) for e in entries),
                   **{key: sum(e['change']==key and e.get('observed',True) for e in entries) for key in ('new','modified','unchanged','deleted','unverified')},
                   'known_excluded':sum(e.get('observed') is False for e in entries), 'excluded_directories': len(excluded)}
        return entries, metrics, excluded
