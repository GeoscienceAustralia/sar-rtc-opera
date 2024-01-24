# opera-RTC-otf
On the fly production of sentinel-1 OPERA RTC backscatter 

# Requirments
- Git
- Docker (for OPERA-rtc envionment)
- Setup expects linux instance (e.g. aws linux ami), packages installed with yum

# Setup
- Add user credentials to the files stored in the credentials folder
    - Earthdata credentials - https://urs.earthdata.nasa.gov/users/new
    - Add these to both credentials_earthdata.yaml and .netrc file
- run install script (note credentials must be set on build, if these change the image will need to be rebuilt)
- if running on a new aws instance, install python 3.9
```bash
sh setup.sh
```
- if running with suitable python install
```bash
sh setup.sh --no-install-python
```


# Instructions
- set scene and data path details in config.yaml
- Change the OPERA-rtc-template.yaml. This is the opera template used for all scenes specified
- run process scripts
```bash
sh run_process.sh
```
