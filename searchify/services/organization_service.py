"""Propose reversible organization. No move, rename, delete or upload executor."""
from datetime import datetime, timezone
from pathlib import Path
import uuid

from searchify.core.scanner import matches, PROTECTED
from searchify.services.review_service import inventory, git_metadata, backup_metadata, safe_path, IMPORTANT

DESTINATIONS={'.pdf':'Documents','.txt':'Documents','.md':'Documents','.csv':'Documents','.docx':'Documents',
              '.png':'Images','.jpg':'Images','.jpeg':'Images','.webp':'Images','.gif':'Images',
              '.zip':'Archives','.tar':'Archives','.gz':'Archives'}


class OrganizationService:
    def __init__(self,registry,max_files=50000,backup_path=None):
        self.registry,self.max_files,self.backup_path=registry,max_files,backup_path

    def propose(self):
        operations=[]; roots=[]; remaining=self.max_files; targets=set()
        for directory in self.registry.directories():
            root=Path(directory['path'])
            files,projects,_,complete=inventory(root,max(0,remaining),recursive=False); remaining-=len(files)
            git=git_metadata(root); backup=backup_metadata(self.backup_path)
            roots.append({'path':safe_path(root),'git':git,'backup':backup,'scan_complete':complete,'scope':'registered-root files only'})
            if root in projects: continue  # Project source layouts require project-specific knowledge.
            for row in files:
                source=row['path']; folder=DESTINATIONS.get(source.suffix.lower())
                if (source.parent!=root or folder is None or row['protected'] or matches(source.name,IMPORTANT)
                        or matches(source.name,PROTECTED)): continue
                destination=root/folder/source.name
                blocked=[]
                if destination.exists() or destination in targets: blocked.append('Destination already exists or is proposed twice; never overwrite.')
                if (root/folder).is_symlink(): blocked.append('Destination folder is a symlink.')
                if git['dirty'] is True: blocked.append('Git has uncommitted or untracked changes.')
                if git['dirty'] is None: blocked.append('Git cleanliness has not been verified.')
                if not backup['verified']: blocked.append('Backup recoverability has not been verified.')
                targets.add(destination)
                operations.append({'id':str(uuid.uuid4()),'action':'proposed_move','source':safe_path(source),
                    'destination':safe_path(destination),'reason':f'A root-level {source.suffix.lower()} file could be grouped in {folder}; its current placement may be intentional.',
                    'approval_required':True,'executable':False,'blockers':blocked,
                    'verification':{'expected_stat_signature':row['signature'],'size_bytes':row['size'],
                                    'requirements':['Recheck source identity and byte hash before executing any approved move.',
                                                    'Verify destination is inside the same registered root and does not exist.',
                                                    'Verify a recoverable backup and review Git changes.']},
                    'rollback':'For an approved move, verify the moved file and return it to the original path only if that path is free. Never overwrite either location.'})
        return {'schema_version':1,'timestamp':datetime.now(timezone.utc).isoformat(),'mode':'proposal_only',
                'roots':roots,'operations':operations,'execution_supported':False,'no_original_files_changed':True}
