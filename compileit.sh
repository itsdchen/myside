#! /usr/bin/env bash

set -e
set -o pipefail
set -o nounset

HOMEDIR=$( dirname -- "$( readlink -f -- "$0"; )");
# Overriding it for jammy builds.
#BUILDDIR=$HOMEDIR"/build"
BUILDDIR="${BUILDDIR_OVERRIDE:-$HOMEDIR/build}"

# Prefer the venv's cmake if present (system cmake on Jammy is 3.22, CMakeLists
# requires >=3.24). Falls back to whatever cmake is on PATH otherwise. Override
# with CMAKE=/path/to/cmake.
CMAKE="${CMAKE:-cmake}"
if [ -x "$HOME/.venvs/v1/bin/cmake" ]; then
    CMAKE="$HOME/.venvs/v1/bin/cmake"
fi


# Make a dir if needed.
function optmkdir() {
if [ ! -d $1 ]
then
    mkdir $1
fi
}

# NOTE: For profiling, I can add a -pg to the cxxflags below:
# -pg
# and do a run like:
# ~/tradefi/pkt_4/bin/pktrade --conf pk_12.json --date 20240906 --full-output --out-dir
# That should create a file like "gmon.out"
# which I can then analyze with:
# gprof pktrade gmon.out > analysis.txt
# Which should then give me the breakdown of time spent.


function build() {
    # Series of gcc flags to speed things up.
    # I'm defining them here instead of in the cmake file because I don't want them enabled for debug builds, and I don't know how to do everything
    # properly for that.
    # Some references:
    # https://gcc.gnu.org/onlinedocs/gcc/Optimize-Options.html
    # https://gcc.gnu.org/wiki/FloatingPointMath
    # https://pspdfkit.com/blog/2021/understanding-fast-math/
    # http://spfrnd.de/posts/2018-03-10-fast-exponential.html
    # o3 is the biggest difference-maker by far.
    # I might find other ones later on but for now we have a signalscribe of ~500 signal instances that decreases runtime from ~3 minutes to ~35s.
    # Counts as a good enough win.
    if [ "$JAMMY" = true ]; then
        BINDIR="$HOMEDIR/jammy.bin"
    elif [ "$NOBLE" = true ]; then
        BINDIR="$HOMEDIR/noble.bin"
    else
        BINDIR="$HOMEDIR/bin"
    fi

    MOLD_FLAG=""
    if command -v mold >/dev/null 2>&1; then
        MOLD_FLAG="-fuse-ld=mold"
    fi
    "$CMAKE" -E env CXXFLAGS="-O3  -fno-math-errno -fno-signed-zeros -ffp-contract=fast -msse4.2" LDFLAGS="$MOLD_FLAG" CMAKE_CXX_COMPILER_LAUNCHER="ccache" CMAKE_C_COMPILER_LAUNCHER="ccache" "$CMAKE" -B $BUILDDIR -DEXECUTABLE_OUTPUT_PATH="$BINDIR" -DCMAKE_POLICY_VERSION_MINIMUM=3.5
    make -C $BUILDDIR -j$(nproc) $@
    echo "Done"
}

function buildd() {
    #Check for builddir
    # Add address sanitizer.

    # Add address sanitizer to it.
    # -DCMAKE_CXX_FLAGS="-fsanitize=address -fno-omit-frame-pointer"  -DCMAKE_LINKER_FLAGS="-fsanitize=address"
    if [ "$JAMMY" = true ]; then
        BINDIR="$HOMEDIR/jammy.bin.debug"
    elif [ "$NOBLE" = true ]; then
        BINDIR="$HOMEDIR/noble.bin.debug"
    else
        BINDIR="$HOMEDIR/bin.debug"
    fi
    "$CMAKE" -B $BUILDDIR -DCMAKE_BUILD_TYPE=Debug -DEXECUTABLE_OUTPUT_PATH="$BINDIR"
    make -C $BUILDDIR -j$(nproc) $@
    echo "Done"
}

function clean() {
    if test -d $BUILDDIR/CMakeFiles ; then
        rm -rf $BUILDDIR/CMakeFiles
    fi

    if test -f $BUILDDIR/CMakeCache.txt ; then
        rm $BUILDDIR/CMakeCache.txt
    fi
}

CLEAN=false
DEBUG=false
JAMMY=false
NOBLE=false
TARGET=""

optstring=":hcdjn"

while getopts ${optstring} arg; do
    case ${arg} in
        h)
	    ;;
        c)
            CLEAN=true
            ;;
        d)
            DEBUG=true
            ;;
        j)
            JAMMY=true
            ;;
        n)
            NOBLE=true
            ;;
        :)
            echo "$0: Must supply an argument to -$OPTARG." >&2
            exit 1
            ;;
        ?)
        echo "Invalid Option -${OPTARG}."
        exit 2
        ;;
    esac
done


echo $TARGET


shift $(($OPTIND - 1))

# This is then the target.
remaining_args="$@"

if [ "$CLEAN" = true ]; then
    echo "Cleaning"
    clean
fi

if [ "$DEBUG" = true ]; then
    echo "Building debug bins"
    buildd $remaining_args
else
    echo "Building"
    build $remaining_args
fi
