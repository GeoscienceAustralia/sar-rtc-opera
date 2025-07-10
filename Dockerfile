FROM oraclelinux:9

LABEL author="OPERA ADT" \
    description="RTC cal/val release R4" \
    version="1.0.4-atmosbugfix"

RUN yum -y update &&\
    yum -y install curl &&\
    yum install git -y &&\
    adduser rtc_user

RUN mkdir -p /home/rtc_user/OPERA/RTC

RUN chmod -R 755 /home/rtc_user &&\
    chown -R rtc_user:rtc_user /home/rtc_user/OPERA

USER rtc_user

ENV CONDA_PREFIX=/home/rtc_user/miniconda3

# install Miniconda
WORKDIR /home/rtc_user
RUN curl -sSL https://repo.continuum.io/miniconda/Miniconda3-latest-Linux-x86_64.sh -o miniconda.sh &&\
    bash miniconda.sh -b -p ${CONDA_PREFIX} &&\
    rm $HOME/miniconda.sh

ENV PATH=${CONDA_PREFIX}/bin:${PATH}
RUN ${CONDA_PREFIX}/bin/conda init bash

# This is currently a fork from main that has updated isce3 package with atmospheric fixes 
# it is RTC==v1.0.4 with isce3==0.24.4 instead of isce3==0.15.0 
RUN git clone --branch software_updates https://github.com/abradley60/RTC.git /home/rtc_user/OPERA/RTC

# create CONDA environment
RUN conda env create --name "RTC" --file /home/rtc_user/OPERA/RTC/Docker/environment.yml &&  conda clean -afy

SHELL ["conda", "run", "-n", "RTC", "/bin/bash", "-c"]

WORKDIR /home/rtc_user/OPERA

# installing OPERA s1-reader
RUN curl -sSL https://github.com/isce-framework/s1-reader/archive/refs/tags/v0.2.5.tar.gz -o s1_reader_src.tar.gz &&\
    tar -xvf s1_reader_src.tar.gz &&\
    ln -s s1-reader-0.2.5 s1-reader &&\
    rm s1_reader_src.tar.gz &&\
    python -m pip install ./s1-reader

# installing OPERA RTC
RUN python -m pip install ./RTC &&\
    echo "conda activate RTC" >> /home/rtc_user/.bashrc

WORKDIR /home/rtc_user/scratch

ENTRYPOINT ["conda", "run", "--no-capture-output", "-n", "RTC"]
