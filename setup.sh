INSTALL_CONDA=false

# Process command-line arguments
while [[ $# -gt 0 ]]; do
    case "$1" in
        -p|--install-conda)
            INSTALL_CONDA=true
            ;;
        -np|--no-install-conda)
            INSTALL_CONDA=false
            ;;
        *)
            echo "Unknown option: $1"
            exit 1
            ;;
    esac
    shift
done

# Python installation, needed on aws image for management
if [ "$INSTALL_CONDA" = true ]; then
    echo "Installing mamba environment manager"
    curl -L -O "https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-$(uname)-$(uname -m).sh"
    bash Miniforge3-$(uname)-$(uname -m).sh -b
    ~/miniforge3/bin/conda init
    source ~/.bashrc
    echo "yes" | conda update --all
    rm ./Miniforge3-$(uname)-$(uname -m).sh
fi

# create the environment
conda env create --file environment.yml

# buld the docker image - see Dockerfile for RTC version
REPO=opera
IMAGE=rtc
TAG=final_1.0.4-atmosbugfix

echo "IMAGE is $REPO/$IMAGE:$TAG"

# fail on any non-zero exit codes
set -ex

# build image
docker build --rm --force-rm --network host -t $REPO/$IMAGE:$TAG -f Dockerfile .