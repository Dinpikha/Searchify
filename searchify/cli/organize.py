"""Display a proposal and its safety blockers; contains no execution path."""
from searchify.cli.output import line
from searchify.cli.workspace import local_paths
from searchify.core.file_registry import FileRegistry
from searchify.services.organization_service import OrganizationService
from searchify.config.settings import settings


def run(args):
    db,_=local_paths()
    plan=OrganizationService(FileRegistry(db),max_files=args.max_files,backup_path=args.backup_path or settings.BACKUP_PATH).propose()
    line('ORGANIZATION PROPOSAL — nothing will be moved, renamed, deleted or uploaded')
    if not plan['operations']: line('No conservative root-level organization proposals from the inspected files.')
    for root in plan['roots']:
        line(root['path']+' — Git: '+root['git']['status']+'; backup: '+root['backup']['status'])
    for operation in plan['operations']:
        line(operation['source']+' -> '+operation['destination'])
        line('  Reason: '+operation['reason'])
        for blocker in operation['blockers']: line('  Blocked: '+blocker)
        for requirement in operation['verification']['requirements']: line('  Verify: '+requirement)
        line('  Rollback: '+operation['rollback'])
    line('Every operation requires explicit approval and fresh verification. This command has no execution mode.')
    if args.save:
        from searchify.reporting.artifacts import save_artifact
        line('Saved proposal: '+str(save_artifact(settings.PLANS_DIR,'organization',plan)))
    return 0
