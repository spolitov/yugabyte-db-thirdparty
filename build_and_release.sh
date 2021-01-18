#!/usr/bin/env bash

set -euo pipefail

# shellcheck source=./yb-thirdparty-common.sh
. "${BASH_SOURCE%/*}/yb-thirdparty-common.sh"

# -------------------------------------------------------------------------------------------------
# OS detection
# -------------------------------------------------------------------------------------------------

if ! "$is_mac"; then
  cat /proc/cpuinfo
fi

# -------------------------------------------------------------------------------------------------
# Display various settings
# -------------------------------------------------------------------------------------------------

# Current user
USER=$(whoami)
log "Current user: $USER"

# PATH
export PATH=/usr/local/bin:$PATH
log "PATH: $PATH"

YB_THIRDPARTY_ARCHIVE_NAME_SUFFIX=${YB_THIRDPARTY_ARCHIVE_NAME_SUFFIX:-}
log "YB_THIRDPARTY_ARCHIVE_NAME_SUFFIX: ${YB_THIRDPARTY_ARCHIVE_NAME_SUFFIX:-undefined}"

YB_BUILD_THIRDPARTY_ARGS=${YB_BUILD_THIRDPARTY_ARGS:-}
log "YB_BUILD_THIRDPARTY_ARGS: ${YB_BUILD_THIRDPARTY_ARGS:-undefined}"

YB_BUILD_THIRDPARTY_EXTRA_ARGS=${YB_BUILD_THIRDPARTY_EXTRA_ARGS:-}
log "YB_BUILD_THIRDPARTY_EXTRA_ARGS: ${YB_BUILD_THIRDPARTY_EXTRA_ARGS:-undefined}"

if [[ -n ${YB_LINUXBREW_DIR:-} ]]; then
  if "$is_mac"; then
    log "Un-setting YB_LINUXBREW_DIR on macOS"
    unset YB_LINUXBREW_DIR
  elif [[ $YB_THIRDPARTY_ARCHIVE_NAME_SUFFIX != *linuxbrew* ]]; then
    log "Un-setting YB_LINUXBREW_DIR for build name $YB_THIRDPARTY_ARCHIVE_NAME_SUFFIX"
    unset YB_LINUXBREW_DIR
  fi
fi
log "YB_LINUXBREW_DIR=${YB_LINUXBREW_DIR:-undefined}"

# -------------------------------------------------------------------------------------------------
# Installed tools
# -------------------------------------------------------------------------------------------------

echo "Bash version: $BASH_VERSION"

tools_to_show_versions=(
  cmake
  automake
  autoconf
  autoreconf
  pkg-config
)

if "$is_mac"; then
  tools_to_show_versions+=( shasum )
elif "$is_centos"; then
  tools_to_show_versions+=( sha256sum libtool )
else
  tools_to_show_versions+=( sha256sum )
fi

for tool_name in "${tools_to_show_versions[@]}"; do
  echo "$tool_name version:"
  ( set -x; "$tool_name" --version )
  echo
done

if cmake --version | grep -E "^cmake version 3.19.1$"; then
  log "CMake 3.19.1 is not supported"
  log "See https://gitlab.kitware.com/cmake/cmake/-/issues/21529 for more details."
  exit 1
fi

# -------------------------------------------------------------------------------------------------
# Check for errors in Python code of this repository
# -------------------------------------------------------------------------------------------------

( set -x; "$YB_THIRDPARTY_DIR/check_python_code.sh" )

# -------------------------------------------------------------------------------------------------

if [[ -n ${CIRCLE_PULL_REQUEST:-} ]]; then
  echo "CIRCLE_PULL_REQUEST is set: $CIRCLE_PULL_REQUEST. Will not upload artifacts."
  unset GITHUB_TOKEN
elif [[ -z ${GITHUB_TOKEN:-} || $GITHUB_TOKEN == *githubToken* ]]; then
  echo "This must be a pull request build. Will not upload artifacts."
  GITHUB_TOKEN=""
else
  echo "This is an official branch build. Will upload artifacts."
fi

# -------------------------------------------------------------------------------------------------

original_repo_dir=$PWD
git_sha1=$( git rev-parse HEAD )
tag=v$( date +%Y%m%d%H%M%S )-${git_sha1:0:10}

archive_dir_name=yugabyte-db-thirdparty-$tag
if [[ -n $YB_THIRDPARTY_ARCHIVE_NAME_SUFFIX ]]; then
  effective_suffix="-$YB_THIRDPARTY_ARCHIVE_NAME_SUFFIX"
else
  effective_suffix="-$os_name"
fi
archive_dir_name+=$effective_suffix
tag+=$effective_suffix

build_dir_parent=/opt/yb-build/thirdparty
repo_dir=$build_dir_parent/$archive_dir_name

( set -x; git remote -v )

origin_url=$( git config --get remote.origin.url )
if [[ -z $origin_url ]]; then
  fatal "Could not get URL of the 'origin' remote in $PWD"
fi

(
  set -x
  mkdir -p "$build_dir_parent"
  git clone "$original_repo_dir" "$repo_dir"
  ( cd "$original_repo_dir" && git diff ) | ( cd "$repo_dir" && patch -p1 )
  cd "$repo_dir"
  git remote set-url origin "$origin_url"
)

if "$is_centos" && [[ $YB_THIRDPARTY_ARCHIVE_NAME_SUFFIX == *linuxbrew* ]]; then
  # Grab a recent URL from https://github.com/YugaByte/brew-build/releases
  brew_url=$(<linuxbrew_url.txt)
  if [[ $brew_url != https://*.tar.gz ]]; then
    fatal "Expected the pre-built Homebrew/Linuxbrew URL to be of the form https://*.tar.gz," \
          "found: $brew_url"
  fi
  brew_tarball_name=${brew_url##*/}
  brew_dir_name=${brew_tarball_name%.tar.gz}
  brew_parent_dir=/opt/yb-build/brew

  export YB_LINUXBREW_DIR=$brew_parent_dir/$brew_dir_name
  if [[ -d $YB_LINUXBREW_DIR ]]; then
    log "Homebrew/Linuxbrew directory already exists at $YB_LINUXBREW_DIR"
  else
    log "Downloading and installing Homebrew/Linuxbrew into a subdirectory of $brew_parent_dir"
    (
      set -x
      mkdir -p "$brew_parent_dir"
      cd "$brew_parent_dir"
      curl --silent -LO "$brew_url"
      time tar xzf "$brew_tarball_name"
    )

    expected_sha256=$( curl --silent -L "$brew_url.sha256" | awk '{print $1}' )
    actual_sha256=$(
      cd "$brew_parent_dir"
      sha256sum "$brew_tarball_name" | awk '{print $1}'
    )
    if [[ $expected_sha256 != "$actual_sha256" ]]; then
      fatal "Invalid SHA256 sum of the Linuxbrew archive: $actual_sha256, expected:" \
            "$expected_sha256"
    fi

    log "Downloaded and installed Homebrew/Linuxbrew to $YB_LINUXBREW_DIR"
    if [[ ! -d $YB_LINUXBREW_DIR ]]; then
      fatal "Directory $YB_LINUXBREW_DIR still does not exist"
    fi

    log "Running post_install.sh"
    (
      cd "$YB_LINUXBREW_DIR"
      time ./post_install.sh
    )
  fi

  log "Linuxbrew gcc version:"
  ( set -x; "$YB_LINUXBREW_DIR/bin/gcc" --version )
fi

echo "Building YugabyteDB third-party code in $repo_dir"

echo "Current directory"
pwd
echo

echo "Free disk space in current directory:"
df -H .
echo

echo "Free disk space on all volumes:"
df -H
echo

cd "$repo_dir"

# We intentionally don't escape variables here so they get split into multiple arguments.
build_thirdparty_cmd_str=./build_thirdparty.sh
if [[ -n ${YB_BUILD_THIRDPARTY_ARGS:-} ]]; then
  build_thirdparty_cmd_str+=" $YB_BUILD_THIRDPARTY_ARGS"
fi

if [[ -n ${YB_BUILD_THIRDPARTY_EXTRA_ARGS:-} ]]; then
  build_thirdparty_cmd_str+=" $YB_BUILD_THIRDPARTY_EXTRA_ARGS"
fi

(
  if [[ -n ${YB_LINUXBREW_DIR:-} ]]; then
    export PATH=$YB_LINUXBREW_DIR/bin:$PATH
  fi
  set -x
  time $build_thirdparty_cmd_str
)

log "Build finished. See timing information above."

# -------------------------------------------------------------------------------------------------
# Cleanup
# -------------------------------------------------------------------------------------------------

( set -x; find . -name "*.pyc" -exec rm -f {} \; )

# -------------------------------------------------------------------------------------------------
# Archive creation and upload
# -------------------------------------------------------------------------------------------------

cd "$build_dir_parent"

archive_tarball_name=$archive_dir_name.tar.gz
archive_tarball_path=$PWD/$archive_tarball_name
if [[ -n ${YB_LINUXBREW_DIR:-} ]]; then
  echo "$YB_LINUXBREW_DIR" >linuxbrew_path.txt
fi

log "Creating archive: $archive_tarball_name"
(
  set -x
  time tar \
    --exclude "$archive_dir_name/.git" \
    --exclude "$archive_dir_name/src" \
    --exclude "$archive_dir_name/build" \
    --exclude "$archive_dir_name/venv" \
    --exclude "$archive_dir_name/download" \
    -czf \
    "$archive_tarball_name" \
    "$archive_dir_name"
)
log "Finished creating archive: $archive_tarball_name. See timing information above."

compute_sha256sum "$archive_tarball_path"
log "Computed SHA256 sum of the archive: $sha256_sum"
echo -n "$sha256_sum" >"$archive_tarball_path.sha256"

if [[ -n ${GITHUB_TOKEN:-} ]]; then
  cd "$repo_dir"
  (
    set -x
    hub release create "$tag" \
      -m "Release $tag" \
      -a "$archive_tarball_path" \
      -a "$archive_tarball_path.sha256" \
      -t "$git_sha1"
  )
else
  log "GITHUB_TOKEN is not set, skipping archive upload"
fi
