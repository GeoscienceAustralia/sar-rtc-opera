git clone https://github.com/opera-adt/RTC.git RTC

# build container
cp -fr build_docker_image_otf.sh RTC
cp -fr Dockerfile RTC/Docker
cp -fr credentials RTC
cd RTC
sh build_docker_image_otf.sh
cd ..

# copy .netrc credentials on local machine
cp -fr credentials/.netrc ~/
chmod og-rw ~/.netrc

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
    echo "Installing Python3.9..."
    wget https://github.com/conda-forge/miniforge/releases/latest/download/Mambaforge-Linux-x86_64.sh
    echo "yes" | Mambaforge-Linux-x86_64.sh -y
fi

# create the environment
conda env create --file environment.yml

# ignore changes to credentials
git update-index --assume-unchanged credentials/*
git update-index --assume-unchanged credentials/.netrc