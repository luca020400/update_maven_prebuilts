#!/usr/bin/python3

"""
Updates prebuilt libraries used by Android builds.
"""
import os, sys, getopt, zipfile, re
import argparse
import glob
import subprocess
from shutil import copyfile, rmtree, which, move, copy, copytree
from distutils.version import LooseVersion
from functools import reduce
import six
import urllib.request, urllib.parse, urllib.error

gmaven_dir = 'gmaven'

temp_dir = os.path.join(os.getcwd(), "support_tmp")
git_dir = os.getcwd()

# See (https://developer.android.com/studio/build/dependencies#gmaven-access)
GMAVEN_BASE_URL = 'https://dl.google.com/dl/android/maven2'

# List of maven artifacts to add to build
# e.g.:
#   androidx.appcompat:appcompat': { }
# Leave map blank to automatically populate name and path:
# - Name format is MAVEN.replaceAll(':','_')
# - Path format is MAVEN.replaceAll(':','/').replaceAll('.','/')
maven_to_make = {
}

# List of artifacts that will be updated from GMaven
# Use pattern: `group:library:version:extension`
# e.g.:
#   androidx.appcompat:appcompat:1.2.0:aar
# Use `latest` to always fetch the latest version.
# e.g.:
#   androidx.appcompat:appcompat:latest:aar
# Also make sure you add `group:library`:{} to maven_to_make as well.
gmaven_artifacts = {
}


# Mapping of POM dependencies to Soong build targets
deps_rewrite = {
    'auto-common':'auto_common',
    'auto-value-annotations':'auto_value_annotations',
    'com.google.auto.value:auto-value':'libauto_value_plugin',
    'monitor':'androidx.test.monitor',
    'rules':'androidx.test.rules',
    'runner':'androidx.test.runner',
    'androidx.test:core':'androidx.test.core',
    'com.squareup:javapoet':'javapoet',
    'com.google.guava:listenablefuture':'guava-listenablefuture-prebuilt-jar',
    'sqlite-jdbc':'xerial-sqlite-jdbc',
    'gson':'gson-prebuilt-jar',
    'com.intellij:annotations':'jetbrains-annotations',
    'javax.annotation:javax.annotation-api':'javax-annotation-api-prebuilt-host-jar',
    'org.robolectric:robolectric':'Robolectric_all-target',
    'org.jetbrains.kotlin:kotlin-stdlib-common':'kotlin-stdlib',
    'org.jetbrains.kotlinx:kotlinx-coroutines-core':'kotlinx_coroutines',
    'org.jetbrains.kotlinx:kotlinx-coroutines-android':'kotlinx_coroutines_android',
    'org.jetbrains.kotlinx:kotlinx-metadata-jvm':'kotlinx_metadata_jvm',
}


def name_for_artifact(group_artifact):
    """Returns the build system target name for a given library's Maven coordinate.

    Args:
        group_artifact: an unversioned Maven artifact coordinate, ex. androidx.core:core
    Returns:
        The build system target name for the artifact, ex. androidx.core_core.
    """
    return group_artifact.replace(':','_')


def path_for_artifact(group_artifact):
    """Returns the file system path for a given library's Maven coordinate.

    Args:
        group_artifact: an unversioned Maven artifact coordinate, ex. androidx.core:core
    Returns:
        The file system path for the artifact, ex. androidx/core/core.
    """
    return group_artifact.replace('.','/').replace(':','/')


# Add automatic entries to maven_to_make.
for key in maven_to_make:
    if ('name' not in maven_to_make[key]):
        maven_to_make[key]['name'] = name_for_artifact(key)
    if ('path' not in maven_to_make[key]):
        maven_to_make[key]['path'] = path_for_artifact(key)

# Always remove these files.
blacklist_files = [
    'annotations.zip',
    'public.txt',
    'R.txt',
    'AndroidManifest.xml',
    os.path.join('libs','noto-emoji-compat-java.jar')
]

artifact_pattern = re.compile(r"^(.+?)-(\d+\.\d+\.\d+(?:-\w+\d+)?(?:-[\d.]+)*)\.(jar|aar)$")


class MavenLibraryInfo:
    def __init__(self, key, group_id, artifact_id, version, dir, repo_dir, file):
        self.key = key
        self.group_id = group_id
        self.artifact_id = artifact_id
        self.version = version
        self.dir = dir
        self.repo_dir = repo_dir
        self.file = file


def print_e(*args, **kwargs):
    print(*args, file=sys.stderr, **kwargs)


def touch(fname, times=None):
    with open(fname, 'a'):
        os.utime(fname, times)


def path(*path_parts):
    return reduce((lambda x, y: os.path.join(x, y)), path_parts)


def flatten(list):
    return reduce((lambda x, y: "%s %s" % (x, y)), list)


def rm(path):
    """Removes the file or directory tree at the specified path, if it exists.

    Args:
        path: Path to remove
    """
    if os.path.isdir(path):
        rmtree(path)
    elif os.path.exists(path):
        os.remove(path)


def mv(src_path, dst_path):
    """Moves the file or directory tree at the source path to the destination path.

    This method does not merge directory contents. If the destination is a directory that already
    exists, it will be removed and replaced by the source. If the destination is rooted at a path
    that does not exist, it will be created.

    Args:
        src_path: Source path
        dst_path: Destination path
    """
    if os.path.exists(dst_path):
        rm(dst_path)
    if not os.path.exists(os.path.dirname(dst_path)):
        os.makedirs(os.path.dirname(dst_path))
    for f in (glob.glob(src_path)):
        if '*' in dst_path:
            dst = os.path.join(os.path.dirname(dst_path), os.path.basename(f))
        else:
            dst = dst_path
        move(f, dst)


def cp(src_path, dst_path):
    """Copies the file or directory tree at the source path to the destination path.

    This method does not merge directory contents. If the destination is a directory that already
    exists, it will be removed and replaced by the source. If the destination is rooted at a path
    that does not exist, it will be created.

    Note that the implementation of this method differs from mv, in that it does not handle "*" in
    the destination path.

    Args:
        src_path: Source path
        dst_path: Destination path
    """
    if os.path.exists(dst_path):
        rm(dst_path)
    if not os.path.exists(os.path.dirname(dst_path)):
        os.makedirs(os.path.dirname(dst_path))
    for f in (glob.glob(src_path)):
        if os.path.isdir(f):
            copytree(f, dst_path)
        else:
            copy(f, dst_path)


def detect_artifacts(maven_repo_dirs):
    maven_lib_info = {}

    # Find the latest revision for each artifact, remove others
    for repo_dir in maven_repo_dirs:
        for root, dirs, files in os.walk(repo_dir):
            for file in files:
                if file[-4:] == ".pom":
                    # Read the POM (hack hack hack).
                    group_id = ''
                    artifact_id = ''
                    version = ''
                    file = os.path.join(root, file)
                    with open(file) as pom_file:
                        for line in pom_file:
                            if line[:11] == '  <groupId>':
                                group_id = line[11:-11]
                            elif line[:14] == '  <artifactId>':
                                artifact_id = line[14:-14]
                            elif line[:11] == '  <version>':
                                version = line[11:-11]
                    if group_id == '' or artifact_id == '' or version == '':
                        print_e('Failed to find Maven artifact data in ' + file)
                        continue

                    # Locate the artifact.
                    artifact_file = file[:-4]
                    if os.path.exists(artifact_file + '.jar'):
                        artifact_file = artifact_file + '.jar'
                    elif os.path.exists(artifact_file + '.aar'):
                        artifact_file = artifact_file + '.aar'
                    else:
                        # This error only occurs for a handful of gradle.plugin artifacts that only
                        # ship POM files, so we probably don't need to log unless we're debugging.
                        # print_e('Failed to find artifact for ' + artifact_file)
                        continue

                    # Make relative to root.
                    artifact_file = artifact_file[len(root) + 1:]

                    # Find the mapping.
                    group_artifact = group_id + ':' + artifact_id
                    if group_artifact in maven_to_make:
                        key = group_artifact
                    elif artifact_id in maven_to_make:
                        key = artifact_id
                    else:
                        # No mapping entry, skip this library.
                        continue

                    # Store the latest version.
                    version = LooseVersion(version)
                    if key not in maven_lib_info \
                            or version > maven_lib_info[key].version:
                        maven_lib_info[key] = MavenLibraryInfo(key, group_id, artifact_id, version,
                                                               root, repo_dir, artifact_file)

    return maven_lib_info


def transform_maven_repos(maven_repo_dirs, transformed_dir, extract_res=True):
    """Transforms a standard Maven repository to be compatible with the Android build system.

    Args:
        maven_repo_dirs: path to local Maven repository
        transformed_dir: relative path for output, ex. androidx
        extract_res: whether to extract Android resources like AndroidManifest.xml from AARs
    Returns:
        True if successful, false otherwise.
    """
    cwd = os.getcwd()
    local_repo = os.path.join(cwd, transformed_dir)
    working_dir = temp_dir

    # Parse artifacts.
    maven_lib_info = detect_artifacts(maven_repo_dirs)

    if not maven_lib_info:
        print_e('Failed to detect artifacts')
        return False

    # Move libraries into the working directory, performing any necessary transformations.
    for info in maven_lib_info.values():
        transform_maven_lib(working_dir, info, extract_res)

    # Generate a single Android.bp that specifies to use all of the above artifacts.
    makefile = os.path.join(working_dir, 'Android.bp')
    with open(makefile, 'w') as f:
        args = ["pom2bp"]
        args.extend(["-sdk-version", "31"])
        args.extend(["-default-min-sdk-version", "24"])
        args.append("-static-deps")
        rewriteNames = sorted([name for name in maven_to_make if ":" in name] + [name for name in maven_to_make if ":" not in name])
        args.extend(["-rewrite=^" + name + "$=" + maven_to_make[name]['name'] for name in rewriteNames])
        args.extend(["-rewrite=^" + key + "$=" + value for key, value in deps_rewrite.items()])
        args.extend(["-extra-static-libs=" + maven_to_make[name]['name'] + "=" + ",".join(sorted(maven_to_make[name]['extra-static-libs'])) for name in maven_to_make if 'extra-static-libs' in maven_to_make[name]])
        args.extend(["-optional-uses-libs=" + maven_to_make[name]['name'] + "=" + ",".join(sorted(maven_to_make[name]['optional-uses-libs'])) for name in maven_to_make if 'optional-uses-libs' in maven_to_make[name]])
        args.extend(["-host=" + name for name in maven_to_make if maven_to_make[name].get('host')])
        args.extend(["-host-and-device=" + name for name in maven_to_make if maven_to_make[name].get('host_and_device')])
        args.extend(["."])
        subprocess.check_call(args, stdout=f, cwd=working_dir)

    # Replace the old directory.
    local_repo = os.path.join(cwd, transformed_dir)
    mv(working_dir, local_repo)
    return True

#
def transform_maven_lib(working_dir, artifact_info, extract_res):
    """Transforms the specified artifact for use in the Android build system.

    Moves relevant files for the artifact represented by artifact_info of type MavenLibraryInfo into
    the appropriate path inside working_dir, unpacking files needed by the build system from AARs.

    Args:
        working_dir: The directory into which the artifact should be moved
        artifact_info: A MavenLibraryInfo representing the library artifact
        extract_res: True to extract resources from AARs, false otherwise.
    """
    # Move library into working dir
    new_dir = os.path.normpath(os.path.join(working_dir, os.path.relpath(artifact_info.dir, artifact_info.repo_dir)))
    mv(artifact_info.dir, new_dir)

    matcher = artifact_pattern.match(artifact_info.file)
    maven_lib_name = artifact_info.key
    maven_lib_vers = matcher.group(2)
    maven_lib_type = artifact_info.file[-3:]

    group_artifact = artifact_info.key
    make_lib_name = maven_to_make[group_artifact]['name']
    make_dir_name = maven_to_make[group_artifact]['path']

    artifact_file = os.path.join(new_dir, artifact_info.file)

    if maven_lib_type == "aar":
        if extract_res:
            target_dir = os.path.join(working_dir, make_dir_name)
            if not os.path.exists(target_dir):
                os.makedirs(target_dir)

            process_aar(artifact_file, target_dir)

        with zipfile.ZipFile(artifact_file) as zip:
            manifests_dir = os.path.join(working_dir, "manifests")
            zip.extract("AndroidManifest.xml", os.path.join(manifests_dir, make_lib_name))


def process_aar(artifact_file, target_dir):
    # Extract AAR file to target_dir.
    with zipfile.ZipFile(artifact_file) as zip:
        zip.extractall(target_dir)

    # Remove classes.jar
    classes_jar = os.path.join(target_dir, "classes.jar")
    if os.path.exists(classes_jar):
        os.remove(classes_jar)

    # Remove or preserve empty dirs.
    for root, dirs, files in os.walk(target_dir):
        for dir in dirs:
            dir_path = os.path.join(root, dir)
            if not os.listdir(dir_path):
                os.rmdir(dir_path)

    # Remove top-level cruft.
    for file in blacklist_files:
        file_path = os.path.join(target_dir, file)
        if os.path.exists(file_path):
            os.remove(file_path)


class GMavenArtifact(object):
    # A map from group:library to the latest available version
    key_versions_map = {}
    def __init__(self, artifact_glob):
        try:
            (group, library, version, ext) = artifact_glob.split(':')
        except ValueError:
            raise ValueError(f'Error in {artifact_glob} expected: group:library:version:ext')

        if not group or not library or not version or not ext:
            raise ValueError(f'Error in {artifact_glob} expected: group:library:version:ext')

        self.group = group
        self.group_path = group.replace('.', '/')
        self.library = library
        self.key = f'{group}:{library}'
        self.version = version
        self.ext = ext

    def get_pom_file_url(self):
        return f'{GMAVEN_BASE_URL}/{self.group_path}/{self.library}/{self.version}/{self.library}-{self.version}.pom'

    def get_artifact_url(self):
        return f'{GMAVEN_BASE_URL}/{self.group_path}/{self.library}/{self.version}/{self.library}-{self.version}.{self.ext}'

    def get_latest_version(self):
        latest_version = GMavenArtifact.key_versions_map[self.key] \
                if self.key in GMavenArtifact.key_versions_map else None

        if not latest_version:
            print(f'Fetching latest version for {self.key}')
            group_index_url = f'{GMAVEN_BASE_URL}/{self.group_path}/group-index.xml'
            import xml.etree.ElementTree as ET
            tree = ET.parse(urllib.request.urlopen(group_index_url))
            root = tree.getroot()
            libraries = root.findall('./*[@versions]')
            for library in libraries:
                key = f'{root.tag}:{library.tag}'
                GMavenArtifact.key_versions_map[key] = library.get('versions').split(',')[-1]
            latest_version = GMavenArtifact.key_versions_map[self.key]
        return latest_version


def fetch_gmaven_artifact(artifact):
    """Fetch a GMaven artifact.

    Downloads a GMaven artifact
    (https://developer.android.com/studio/build/dependencies#gmaven-access)

    Args:
        artifact_glob: an instance of GMavenArtifact.
    """
    download_to = os.path.join('gmaven', artifact.group, artifact.library, artifact.version)

    _DownloadFileToDisk(artifact.get_pom_file_url(), os.path.join(download_to, f'{artifact.library}-{artifact.version}.pom'))
    _DownloadFileToDisk(artifact.get_artifact_url(), os.path.join(download_to, f'{artifact.library}-{artifact.version}.{artifact.ext}'))

    return download_to


def _DownloadFileToDisk(url, filepath):
    """Download the file at URL to the location dictated by the path.

    Args:
        url: Remote URL to download file from.
        filepath: Filesystem path to write the file to.
    """
    print(f'Downloading URL: {url}')
    file_data = urllib.request.urlopen(url)

    try:
        os.makedirs(os.path.dirname(filepath))
    except os.error:
        # This is a common situation - os.makedirs fails if dir already exists.
        pass
    try:
        with open(filepath, 'wb') as f:
            f.write(six.ensure_binary(file_data.read()))
    except:
        os.remove(os.path.dirname(filepath))
        raise


def update_gmaven(gmaven_artifacts):
    artifacts = [GMavenArtifact(artifact) for artifact in gmaven_artifacts]
    for artifact in artifacts:
        if artifact.version == 'latest':
            artifact.version = artifact.get_latest_version()

    artifact_dirs = [fetch_gmaven_artifact(artifact) for artifact in artifacts]
    print(artifacts)
    if not transform_maven_repos(['gmaven'], gmaven_dir, extract_res=False):
        return []
    return [artifact.key for artifact in artifacts]


def append(text, more_text):
    if text:
        return "%s, %s" % (text, more_text)
    return more_text


def uncommittedChangesExist():
    try:
        # Make sure we don't overwrite any pending changes.
        diffCommand = "cd " + git_dir + " && git diff --quiet"
        subprocess.check_call(diffCommand, shell=True)
        subprocess.check_call(diffCommand + " --cached", shell=True)
        return False
    except subprocess.CalledProcessError:
        return True


rm(temp_dir)
parser = argparse.ArgumentParser(
    description=('Update current prebuilts'))
parser.add_argument(
    '--commit-first', action="store_true",
    help='If specified, then if uncommited changes exist, commit before continuing')
args = parser.parse_args()
args.file = True
if which('pom2bp') is None:
    parser.error("Cannot find pom2bp in path; please run lunch to set up build environment. You may also need to run 'm pom2bp' if it hasn't been built already.")
    sys.exit(1)

if uncommittedChangesExist():
    if args.commit_first:
        subprocess.check_call("cd " + git_dir + " && git add -u", shell=True)
        subprocess.check_call("cd " + git_dir + " && git commit -m 'save working state'", shell=True)

if uncommittedChangesExist():
    print_e('FAIL: There are uncommitted changes here. Please commit or stash before continuing, because %s will run "git reset --hard" if execution fails' % os.path.basename(__file__))
    sys.exit(1)

try:
    components = None
    updated_artifacts = update_gmaven(gmaven_artifacts)
    if updated_artifacts:
        components = append(components, '\n'.join(updated_artifacts))
    else:
        print_e('Failed to update GMaven, aborting...')
        sys.exit(1)

    subprocess.check_call(['git', 'add', gmaven_dir])
    msg = "Import %s from GMaven\n\n%s" % (components, flatten(sys.argv))
    subprocess.check_call(['git', 'commit', '-q', '-m', msg])
    print('Created commit:')
    subprocess.check_call(['git', 'log', '-1', '--oneline'])
    print('Remember to test this change before uploading it to Gerrit!')

finally:
    # Revert all stray files, including the downloaded zip.
    try:
        with open(os.devnull, 'w') as bitbucket:
            subprocess.check_call(['git', 'add', '-Af', '.'], stdout=bitbucket)
            subprocess.check_call(
                ['git', 'commit', '-m', 'COMMIT TO REVERT - RESET ME!!!', '--allow-empty'], stdout=bitbucket)
            subprocess.check_call(['git', 'reset', '--hard', 'HEAD~1'], stdout=bitbucket)
    except subprocess.CalledProcessError:
        print_e('ERROR: Failed cleaning up, manual cleanup required!!!')
