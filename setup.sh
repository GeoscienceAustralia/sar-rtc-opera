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

INSTALL_PYTHON=false

# Process command-line arguments
while [[ $# -gt 0 ]]; do
    case "$1" in
        -p|--install-python)
            INSTALL_PYTHON=true
            ;;
        -np|--no-install-python)
            INSTALL_PYTHON=false
            ;;
        *)
            echo "Unknown option: $1"
            exit 1
            ;;
    esac
    shift
done

# Python installation, needed on aws image for management
if [ "$INSTALL_PYTHON" = true ]; then
    echo "Installing Python3.9..."
    # Your Python installation commands here
    sudo yum install gcc openssl-devel bzip2-devel libffi-devel zlib-devel
    wget https://www.python.org/ftp/python/3.9.6/Python-3.9.6.tgz
    tar -xvf Python-3.9.6.tgz
    cd Python-3.9.6
    ./configure --enable-optimizations
    sudo make
    sudo make altinstall
    python3.9 --version
    cd ..
    sudo rm -r Python-3.9.6
    sudo rm -r Python-3.9.6.tgz

    # make virtual env for downloads
    source ~/.bashrc
    python3.9 -m pip install virtualenv
    python3.9 -m venv rtc_otf_env
    source rtc_otf_env/bin/activate
    pip install -r requirements.txt
fi

if [ "$INSTALL_PYTHON" = false ]; then
    # make virtual env for downloads
    source ~/.bashrc
    python3 -m pip install virtualenv
    python3 -m venv rtc_otf_env
    source rtc_otf_env/bin/activate
    python3 -m pip install -r requirements.txt
fi

# ignore changes to credentials
git update-index --assume-unchanged credentials/*
git update-index --assume-unchanged credentials/.netrc
