# Maintainer: Erik Johnson <erik at saltstack dot com>
#
# buildpackage.py
#
# WARNING: This script will recursively remove the dest_dir (by default,
# /tmp/saltpkg).
#
# This script is designed for speed, therefore it does not use mock, does not
# run tests, and will install the build deps on the machine running the script.
#

import errno
import glob
import logging
import os
import re
import shutil
import subprocess
import sys
from optparse import OptionParser, OptionGroup

logging.QUIET = 0
logging.GARBAGE = 1
logging.TRACE = 5

logging.addLevelName(logging.QUIET, 'QUIET')
logging.addLevelName(logging.TRACE, 'TRACE')
logging.addLevelName(logging.GARBAGE, 'GARBAGE')

LOG_LEVELS = {
    'all': logging.NOTSET,
    'debug': logging.DEBUG,
    'error': logging.ERROR,
    'critical': logging.CRITICAL,
    'garbage': logging.GARBAGE,
    'info': logging.INFO,
    'quiet': logging.QUIET,
    'trace': logging.TRACE,
    'warning': logging.WARNING,
}

################################# FUNCTIONS ##################################


def _abort(msg):
    '''
    Unrecoverable error, pull the plug
    '''
    log.error(msg)
    global parser
    parser.print_help()
    sys.exit(1)


############################## HELPER FUNCTIONS ##############################

def _init():
    '''
    Parse CLI options
    '''
    global parser
    parser = OptionParser()
    parser.add_option('--platform',
                      dest='platform',
                      help='Platform (\'os\' grain)')
    parser.add_option('--source-dir',
                    dest='source_dir',
                    default='/testing',
                    help='Source directory. Must be a git checkout. '
                         'Default: %default')
    parser.add_option('--dest-dir',
                      dest='dest_dir',
                      default='/tmp/saltpkg',
                      help='Destination directory, will be removed if it '
                           'exists prior to running script. '
                           'Default: %default')
    parser.add_option('--artifact-dir',
                      dest='artifact_dir',
                      default='/tmp/build_artifacts',
                      help='Location where build artifacts should be placed, '
                           'the jenkins master will grab all of these. '
                           'Default: %default')
    parser.add_option('--log-file',
                      dest='log_file',
                      default='/tmp/salt-buildpackage.log',
                      help='Log results to a file. Default: %default')
    parser.add_option('--log-level',
                      dest='log_level',
                      default='warning',
                      help='Control verbosity of logging. Default: %default')
    # RPM option group
    group = OptionGroup(parser, 'RPM-specific Options')
    group.add_option('--spec',
                     dest='spec_file',
                     default='/tmp/salt.spec',
                     help='Spec file to use as a template to build RPM. '
                          'Default: %default')
    parser.add_option_group(group)

    opts = parser.parse_args()[0]

    # Sanity checks
    problems = []
    if not opts.platform:
        problems.append('Platform (\'os\' grain) required')
    if not os.path.isdir(opts.source_dir):
        problems.append('Source directory {0} not found'
                        .format(opts.source_dir))
    try:
        shutil.rmtree(opts.dest_dir)
    except OSError as exc:
        if exc.errno != errno.ENOTDIR:
            problems.append('Unable to remove pre-existing destination '
                            'directory {0}: {1}'.format(opts.dest_dir, exc))
    finally:
        try:
            os.makedirs(opts.dest_dir)
        except OSError as exc:
            problems.append('Unable to create destination directory {0}: {1}'
                            .format(opts.dest_dir, exc))
    try:
        shutil.rmtree(opts.artifact_dir)
    except OSError as exc:
        if exc.errno != errno.ENOTDIR:
            problems.append('Unable to remove pre-existing artifact directory '
                            '{0}: {1}'.format(opts.artifact_dir, exc))
    finally:
        try:
            os.makedirs(opts.artifact_dir)
        except OSError as exc:
            problems.append('Unable to create artifact directory {0}: {1}'
                            .format(opts.artifact_dir, exc))

    return opts, problems


def _move(src, dst):
    '''
    Wrapper around shutil.move()
    '''
    try:
        os.remove(os.path.join(dst, os.path.basename(src)))
    except OSError as exc:
        if exc.errno != errno.ENOENT:
            _abort(exc)

    try:
        shutil.move(src, dst)
    except shutil.Error as exc:
        _abort(exc)


def _run_command(args):
    log.info('Running command: {0}'.format(args))
    proc = subprocess.Popen(args,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE)
    stdout, stderr = proc.communicate()
    if stdout:
        log.debug('Command output: \n{0}'.format(stdout))
    if stderr:
        log.error(stderr)
    log.info('Return code: {0}'.format(proc.returncode))
    return stdout, stderr, proc.returncode


def _make_sdist(opts, python_bin='python'):
    os.chdir(opts.source_dir)
    stdout, stderr, rcode = _run_command([python_bin, 'setup.py', 'sdist'])
    if rcode == 0:
        # Find the sdist with the most recently-modified metadata
        sdist_path = max(
            glob.iglob(os.path.join(opts.source_dir, 'dist', 'salt-*.tar.gz')),
            key=os.path.getctime
        )
        log.info('sdist is located at {0}'.format(sdist_path))
        return sdist_path
    else:
        _abort('Failed to create sdist')


############################# BUILDER FUNCTIONS ##############################


def build_centos(opts):
    '''
    Build an RPM
    '''
    log.info('Building CentOS RPM')
    log.info('Detecting major release')
    try:
        with open('/etc/redhat-release', 'r') as fp_:
            redhat_release = fp_.read().strip()
            major_release = int(redhat_release.split()[2].split('.')[0])
    except (ValueError, IndexError):
        _abort('Unable to determine major release from /etc/redhat-release '
               'contents: {0!r}'.format(redhat_release))
    except IOError as exc:
        _abort('{0}'.format(exc))

    log.info('major_release: {0}'.format(major_release))

    define_opts = [
        '--define',
        '_topdir {0}'.format(os.path.join(opts.dest_dir))
    ]
    build_reqs = ['rpm-build']
    if major_release == 5:
        python_bin = 'python26'
        define_opts.extend(['--define=', 'dist .el5'])
        build_reqs.extend(['python26-devel'])
    elif major_release == 6:
        build_reqs.extend(['python-devel'])
    elif major_release == 7:
        build_reqs.extend(['python-devel', 'systemd-units'])
    else:
        _abort('Unsupported major release: {0}'.format(major_release))

    # Install build deps
    _run_command(['yum', '-y', 'install'] + build_reqs)

    # Make the sdist
    try:
        sdist = _make_sdist(opts, python_bin=python_bin)
    except NameError:
        sdist = _make_sdist(opts)

    # Example tarball names:
    #   - Git checkout: salt-2014.7.0rc1-1584-g666602e.tar.gz
    #   - Tagged release: salt-2014.7.0.tar.gz
    tarball_re = re.compile('^salt-([^-]+)(?:-(\d+)-(g[0-9a-f]+))?\.tar\.gz$')
    try:
        base, offset, oid = tarball_re.match(os.path.basename(sdist)).groups()
    except AttributeError:
        _abort('Unable to extract version info from sdist filename {0!r}'
               .format(sdist))

    if offset is None:
        salt_pkgver = salt_srcver = base
    else:
        salt_pkgver = '.'.join((base, offset, oid))
        salt_srcver = '-'.join((base, offset, oid))

    log.info('salt_pkgver: {0}'.format(salt_pkgver))
    log.info('salt_srcver: {0}'.format(salt_srcver))

    # Setup build environment
    for dest_dir in 'BUILD BUILDROOT RPMS SOURCES SPECS SRPMS'.split():
        path = os.path.join(opts.dest_dir, dest_dir)
        try:
            os.makedirs(path)
        except OSError:
            pass
        if not os.path.isdir(path):
            _abort('Unable to make directory: {0}'.format(path))

    # Get sources into place
    build_sources_path = os.path.join(opts.dest_dir, 'SOURCES')
    rpm_sources_path = os.path.join(opts.source_dir, 'pkg', 'rpm')
    _move(sdist, build_sources_path)
    for src in ('salt-master', 'salt-syndic', 'salt-minion', 'salt-api',
                'salt-master.service', 'salt-syndic.service',
                'salt-minion.service', 'salt-api.service',
                'README.fedora', 'logrotate.salt'):
        shutil.copy(os.path.join(rpm_sources_path, src), build_sources_path)

    # Prepare SPEC file
    spec_path = os.path.join(opts.dest_dir, 'SPECS', 'salt.spec')
    with open(opts.spec_file, 'r') as spec:
        spec_lines = spec.read().splitlines()
    with open(spec_path, 'w') as fp_:
        for line in spec_lines:
            if line.startswith('%global srcver '):
                line = '%global srcver {0}'.format(salt_srcver)
            elif line.startswith('Version: '):
                line = 'Version: {0}'.format(salt_pkgver)
            fp_.write(line + '\n')

    # Do the thing
    cmd = ['rpmbuild', '-bb']
    cmd.extend(define_opts)
    cmd.append(spec_path)
    stdout, stderr, rcode = _run_command(cmd)

    if rcode != 0:
        _abort('Build failed.')

    return glob.glob(
        os.path.join(
            opts.dest_dir,
            'RPMS',
            'noarch',
            'salt-*{0}*.noarch.rpm'.format(salt_pkgver)
        )
    )


#################################### MAIN ####################################

if __name__ == '__main__':
    opts, problems = _init()

    # Setup logging
    log_format = '%(asctime)s.%(msecs)03d %(levelname)s: %(message)s'
    log_datefmt = '%H:%M:%S'
    log_level = LOG_LEVELS[opts.log_level] \
        if opts.log_level in LOG_LEVELS \
        else LOG_LEVELS['warning']
    logging.basicConfig(filename=opts.log_file,
                        format=log_format,
                        datefmt=log_datefmt,
                        level=LOG_LEVELS[opts.log_level])
    log = logging.getLogger(__name__)
    if opts.log_level not in LOG_LEVELS:
        log.error('Invalid log level {0!r}, falling back to \'warning\''
                  .format(opts.log_level))

    if problems:
        for problem in problems:
            log.error(problem)
        sys.exit(1)

    # Build for the specified platform
    if not opts.platform:
        _abort('Platform required')
    elif opts.platform.lower() == 'centos':
        artifacts = build_centos(opts)
    else:
        _abort('Unsupported platform {0!r}'.format(opts.platform))

    log.info('Build complete. Artifacts will be stored in {0}'
             .format(opts.artifact_dir))
    for artifact in artifacts:
        shutil.copy(artifact, opts.artifact_dir)
        log.info('Copied {0} to artifact directory'.format(artifact))
    log.info('Done!')