git clone https://github.com/opera-adt/RTC.git RTC

# build container
cp -fr build_docker_image_otf.sh RTC
cp -fr Dockerfile RTC/Docker
cp -fr credentials/.netrc RTC
cd RTC
sh build_docker_image_otf.sh
cd ..

# make virtual env for downloads
source ~/.bashrc
pip install virtualenv
python -m venv rtc_otf_env
source rtc_otf_env/bin/activate
pip install -r requirements.txt

