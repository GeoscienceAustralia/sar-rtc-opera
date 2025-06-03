# This is currently a fork from main that has updated isce3 package with atmospheric fixes 
# it is RTC==v1.0.4 with isce3==0.24.4 instead of isce3==0.15.0 
git clone --branch software_updates https://github.com/abradley60/RTC.git RTC

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
    echo "Installing mamba environment manager"
    wget https://github.com/conda-forge/miniforge/releases/latest/download/Mambaforge-Linux-x86_64.sh
    bash ./Mambaforge-Linux-x86_64.sh -b -f -p ~/mambaforge
    ~/mambaforge/bin/conda init
    source ~/.bashrc
    echo "yes" | conda update --all
    rm ./Mambaforge-Linux-x86_64.sh
fi

# create the environment
conda env create --file environment.yml