import os
import numpy as np
import time

# from .xrf_netcdf import read_xrf_netcdf
# from .xsp3_hhd5 import read_xsp3_hdf5
# from .asciifiles import readMasterFile

import larch
from larch_plugins.xrmmap import (read_xrf_netcdf,
                                   read_xsp3_hdf5, readASCII,
                                   readMasterFile, readROIFile,
                                   readEnvironFile, parseEnviron,
                                   read_xrd_netcdf, read_xrd_hdf5)

from larch_plugins.xrd import integrate_xrd_row

class GSEXRM_FileStatus:
    no_xrfmap    = 'hdf5 does not have top-level XRF map'
    no_xrdmap    = 'hdf5 does not have top-level XRD map'
    created      = 'hdf5 has empty schema'  # xrm map exists, no data
    hasdata      = 'hdf5 has map data'      # array sizes known
    wrongfolder  = 'hdf5 exists, but does not match folder name'
    err_notfound = 'file not found'
    empty        = 'file is empty (read from folder)'
    err_nothdf5  = 'file is not hdf5 (or cannot be read)'


class GSEXRM_Exception(Exception):
    '''GSEXRM Exception: General Errors'''
    pass


class GSEXRM_Detector(object):
    '''Detector class, representing 1 detector element (real or virtual)
    has the following properties (many of these as runtime-calculated properties)

    rois           list of ROI objects
    rois[i].name        names
    rois[i].address     address
    rois[i].left        index of lower limit
    rois[i].right       index of upper limit
    energy         array of energy values
    counts         array of count values
    dtfactor       array of deadtime factor
    realtime       array of real time
    livetime       array of live time
    inputcounts    array of input counts
    outputcount    array of output count

    '''
    def __init__(self, xrmmap, index=None):
        self.xrmmap = xrmmap
        self.__ndet =  xrmmap.attrs.get('N_Detectors', 0)
        self.det = None
        self.rois = []
        detname = 'det1'
        if index is not None:
            self.det = self.xrmmap['det%i' % index]
            detname = 'det%i' % index

        self.shape =  self.xrmmap['%s/livetime' % detname].shape

        # energy
        self.energy = self.xrmmap['%s/energy' % detname].value

        # set up rois
        rnames = self.xrmmap['%s/roi_names' % detname].value
        raddrs = self.xrmmap['%s/roi_addrs' % detname].value
        rlims  = self.xrmmap['%s/roi_limits' % detname].value
        for name, addr, lims in zip(rnames, raddrs, rlims):
            self.rois.append(ROI(name=name, address=addr,
                                 left=lims[0], right=lims[1]))

    def __getval(self, param):
        if self.det is None:
            out = self.xrmmap['det1/%s' % (param)].value
            for i in range(2, self.__ndet):
                out += self.xrmmap['det%i/%s' % (i, param)].value
            return out
        return self.det[param].value

    @property
    def counts(self):
        "detector counts array"
        return self.__getval('counts')

    @property
    def dtfactor(self):
        '''deadtime factor'''
        return self.__getval('dtfactor')

    @property
    def realtime(self):
        '''real time'''
        return self.__getval('realtime')

    @property
    def livetime(self):
        '''live time'''
        return self.__getval('livetime')

    @property
    def inputcounts(self):
        '''inputcounts'''
        return self.__getval('inputcounts')

    @property
    def outputcount(self):
        '''output counts'''
        return self.__getval('outputcounts')


class GSEXRM_Area(object):
    '''Map Area class, representing a map area for a detector
    '''
    def __init__(self, xrmmap, index, det=None):
        self.xrmmap = xrmmap
        self.det = GSEXRM_Detector(xrmmap, index=det)
        if isinstance(index, int):
            index = 'area_%3.3i' % index
        self._area = self.xrmmap['areas/%s' % index]
        self.npts = self._area.value.sum()

        sy, sx = [slice(min(_a), max(_a)+1) for _a in np.where(self._area)]
        self.yslice, self.xslice = sy, sx

    def roicounts(self, roiname):
        iroi = -1
        for ir, roi in enumerate(self.det.rois):
            if roiname.lower() == roi.name.lower():
                iroi = ir
                break
        if iroi < 0:
            raise ValueError('ROI name %s not found' % roiname)
        elo, ehi = self.det.rois[iroi].left, self.det.rois[iroi].right
        counts = self.det.counts[self.yslice, self.xslice, elo:ehi]


class GSEXRM_MapRow:
    '''
    read one row worth of data:
    '''
    def __init__(self, yvalue, xrffile, xrdfile, xpsfile, sisfile, folder,
                 reverse=None, ixaddr=0, dimension=2, ioffset=0,
                 npts=None,  irow=None, dtime=None, nrows_expected=None,
                 masterfile=None, xrftype=None, xrdtype=None,
                 xrdcal=None, xrd2dmask=None, xrd2dbkgd=None,
                 wdg=0, steps=4096, flip=True,
                 has_xrf=True, has_xrd2d=False, has_xrd1d=False):


        self.read_ok = False
        self.nrows_expected = nrows_expected

        ioff = ioffset
        offslice = slice(None, None, None)
        if ioff > 0:
            offslice = slice(ioff, None, None)
        elif ioff < 0:
            offslice = slice(None, ioff, None)

        self.npts = npts
        self.irow = irow
        self.yvalue  = yvalue
        self.xrffile = xrffile
        self.xpsfile = xpsfile
        self.sisfile = sisfile
        self.xrdfile = xrdfile

        self.xrd2d     = None
        self.xrdq      = None
        self.xrd1d     = None
        self.xrdq_wdg  = None
        self.xrd1d_wdg = None

        if masterfile is not None:
            header, rows = readMasterFile(masterfile)
            for row in header:
                if row.startswith('#XRF.filetype'): xrftype = row.split()[-1]
                if row.startswith('#XRD.filetype'): xrdtype = row.split()[-1]
        if has_xrf:
            if xrftype is None:
                xrftype = 'netcdf'
                if xrffile.startswith('xsp3'):
                    xrftype = 'hdf5'
            if xrftype == 'netcdf':
                xrf_reader = read_xrf_netcdf
            else:
                xrf_reader = read_xsp3_hdf5

        if has_xrd2d or has_xrd1d:
            if xrdtype == 'hdf5' or xrdfile.endswith('.h5'):
                xrd_reader = read_xrd_hdf5
            elif xrdtype == 'netcdf' or xrdfile.endswith('nc'):
                xrd_reader = read_xrd_netcdf
            else:
                xrd_reader = read_xrd_netcdf

        # print( "xrd_reader ", xrd_reader, xrdfile, ' cal :%s: ' % xrdcal)
        # reading can fail with IOError, generally meaning the file isn't
        # ready for read.  Try again for up to 5 seconds
        t0 = time.time()
        sis_ok, xps_ok = False, False

        gdata, sdata = [], []
        while not (sis_ok and xps_ok):
            try:
                ghead, gdata = readASCII(os.path.join(folder, xpsfile))
                xps_ok = len(gdata) > 1
            except IOError:
                if (time.time() - t0) > 5.0:
                    break
                time.sleep(0.25)
            try:
                shead, sdata = readASCII(os.path.join(folder, sisfile))
                sdata = sdata[offslice]
                sis_ok = len(sdata) > 1
            except IOError:
                if (time.time() - t0) > 5.0:
                    break
                time.sleep(0.25)

        if not(sis_ok and xps_ok):
            print('Failed to read ASCII data for SIS: %s (%i), XPS: %s (%i)' %
                     (sisfile, len(sdata), xpsfile, len(gdata)) )
            return

        # extrapolate gathering data by in case final end-point trigger was missed
        gather_extra = (2*gdata[-1] - gdata[-2]).reshape((1, gdata.shape[1]))
        gdata = np.concatenate((gdata, gather_extra))
        gnpts, ngather  = gdata.shape

        self.sishead = shead
        if dtime is not None:  dtime.add('maprow: read ascii files')
        t0 = time.time()

        atime = -1

        xrf_dat, xrf_file = None, os.path.join(folder, xrffile)
        xrd_dat, xrd_file = None, os.path.join(folder, xrdfile)

        while atime < 0 and time.time()-t0 < 10:
            try:
                atime = os.stat(os.path.join(folder, sisfile)).st_ctime
                if has_xrf:
                    xrf_dat = xrf_reader(xrf_file, npixels=self.nrows_expected, verbose=False)
                    if xrf_dat is None:
                        print( 'Failed to read XRF data from %s' % self.xrffile)
                if has_xrd2d or has_xrd1d:
                    xrd_dat = xrd_reader(xrd_file, verbose=False)
                    if xrd_dat is None:
                        print( 'Failed to read XRD data from %s' % self.xrdfile)

            except (IOError, IndexError):
                time.sleep(0.010)

        if atime < 0:
            print( 'Failed to read data.')
            return
        if dtime is not None:
            dtime.add('maprow: read XRM files')

        ## SPECIFIC TO XRF data
        if has_xrf:
            self.counts    = xrf_dat.counts[offslice]
            self.inpcounts = xrf_dat.inputCounts[offslice]
            self.outcounts = xrf_dat.outputCounts[offslice]

            if self.inpcounts.max() < 1:
                self.inpcounts = self.counts.sum(axis=2)
            if self.outcounts.max() < 1:
                self.outcounts = self.inpcounts*1.0

            self.livetime  = xrf_dat.liveTime[offslice]
            self.realtime  = xrf_dat.realTime[offslice]
            if self.livetime.max() < 0.01:
                self.livetime = 0.100 * np.ones(self.livetime.shape)
            if self.realtime.max() < 0.01:
                self.realtime = 0.100 * np.ones(self.realtime.shape)

            dt_denom = self.outcounts*self.livetime
            dt_denom[np.where(dt_denom < 1)] = 1.0
            self.dtfactor  = self.inpcounts*self.realtime/dt_denom
            self.dtfactor[np.where(self.dtfactor < 0.5)] = 0.5

        ## SPECIFIC TO XRD data
        if has_xrd2d or has_xrd1d:
            if self.npts == xrd_dat.shape[0]:
                self.xrd2d = xrd_dat
            elif self.npts > xrd_dat.shape[0]:
                self.xrd2d = np.zeros((self.npts,xrd_dat.shape[1],xrd_dat.shape[2]))
                self.xrd2d[0:xrd_dat.shape[0]] = xrd_dat
            else:
                self.xrd2d = xrd_dat[0:self.npts]

            ############################################################################
            ## subtracts background and applies mask, row by row
            ## mkak 2018.02.01
            ## major speed up if no background or mask specified
            ## updated mkak 2018.03.30

            if xrd2dmask is not None:
                dir = -1 if flip else 1
                mask2d = np.ones(self.xrd2d[0].shape)
                mask2d = mask2d - xrd2dmask[::dir]
                if xrd2dbkgd is not None:
                    self.xrd2d = mask2d*(self.xrd2d-xrd2dbkgd)
                else:
                    self.xrd2d = mask2d*(self.xrd2d)
            elif xrd2dbkgd is not None:
                self.xrd2d = self.xrd2d-xrd2dbkgd

            ## limits all values to positive
            self.xrd2d[self.xrd2d < 0] = 0
            ############################################################################

            if has_xrd1d and xrdcal is not None:
                attrs = dict(steps=steps, flip=flip)
                if 'eig' in xrd_file:
                    # look for pre-integrated data from eiger
                    x1dfile = xrd_file.replace('.h5', '.npy').replace('_master', '')
                    if os.path.exists(x1dfile):
                        xdat = np.load(x1dfile)
                        self.xrdq  = xdat[0, :]
                        self.xrd1d = xdat[1:, :]
                if self.xrdq is None: # integrate data if needed.
                    attrs['flip'] = True
                    self.xrd2d = self.xrd2d[:, 1:-1, 3:-3]
                    maxval = 2**32 - 2**14
                    self.xrd2d[np.where(self.xrd2d>maxval)] = 0
                    self.xrdq, self.xrd1d = integrate_xrd_row(self.xrd2d, xrdcal,
                                                              **attrs)
                if wdg > 1:
                    self.xrdq_wdg, self.xrd1d_wdg = [], []
                    wdg_sz = 360./int(wdg)
                    for iwdg in range(wdg):
                        wdg_lmts = np.array([iwdg*wdg_sz, (iwdg+1)*wdg_sz]) - 180
                        attrs.update({'wedge_limits':wdg_lmts})
                        q, counts = integrate_xrd_row(self.xrd2d, xrdcal, **attrs)
                        self.xrdq_wdg  += [q]
                        self.xrd1d_wdg += [counts]

                    self.xrdq_wdg  = np.einsum('kij->ijk', self.xrdq_wdg)
                    self.xrd1d_wdg = np.einsum('kij->ijk', self.xrd1d_wdg)


        xnpts, nmca = gnpts, 1
        if has_xrf:
            xnpts, nmca, nchan = self.counts.shape

        snpts, nscalers = sdata.shape

        # print("Row npts=%s, gather=%d, sis=%d, xrf=%d" %
        #       (repr(self.npts), gnpts, snpts, xnpts))

        if self.npts is None:
            self.npts = min(gnpts, xnpts)

        if snpts < self.npts:  # extend struck data if needed
            print('     extending SIS data from %i to %i !' % (snpts, self.npts))
            sdata = list(sdata)
            for i in range(self.npts+1-snpts):
                sdata.append(sdata[snpts-1])
            sdata = np.array(sdata)
            snpts = self.npts
        self.sisdata = sdata[:self.npts]

        if xnpts > self.npts:
            if has_xrf:
                self.counts    = self.counts[:self.npts]
                self.realtime  = self.realtime[:self.npts]
                self.livetime  = self.livetime[:self.npts]
                self.dtfactor  = self.dtfactor[:self.npts]
                self.inpcounts = self.inpcounts[:self.npts]
                self.outcounts = self.outcounts[:self.npts]
            if has_xrd2d:
                self.xrd2d = self.xrd2d[:self.npts]
            if has_xrd1d:
                self.xrdq,self.xrd1d = self.xrdq[:self.npts],self.xrd1d[:self.npts]
                if self.xrdq_wdg is not None:
                    self.xrdq_wdg    = self.xrdq_wdg[:self.npts]
                    self.xrd1d_wdg   = self.xrd1d_wdg[:self.npts]

        points = list(range(1, self.npts+1))
        # auto-reverse: counter-intuitively (because stage is upside-down and so
        # backwards wrt optical view), left-to-right scans from high to low value
        # so reverse those that go from low to high value
        if reverse is None:
            reverse = gdata[0, 0] < gdata[-1, 0]

        if reverse:
            points.reverse()
            self.sisdata  = self.sisdata[::-1]
            if has_xrf:
                self.counts  = self.counts[::-1]
                self.realtime = self.realtime[::-1]
                self.livetime = self.livetime[::-1]
                self.dtfactor = self.dtfactor[::-1]
                self.inpcounts= self.inpcounts[::-1]
                self.outcounts= self.outcounts[::-1]
            if has_xrd2d:
                self.xrd2d = self.xrd2d[::-1]
            if has_xrd1d:
                self.xrdq, self.xrd1d = self.xrdq, self.xrd1d[::-1]
                if self.xrdq_wdg is not None:
                    self.xrdq_wdg        = self.xrdq_wdg[::-1]
                    self.xrd1d_wdg       = self.xrd1d_wdg[::-1]

        if has_xrf:
            xvals = [(gdata[i, ixaddr] + gdata[i-1, ixaddr])/2.0 for i in points]
            self.posvals = [np.array(xvals)]
            if dimension == 2:
                self.posvals.append(np.array([float(yvalue) for i in points]))
            self.posvals.append(self.realtime.sum(axis=1).astype('float32') / nmca)
            self.posvals.append(self.livetime.sum(axis=1).astype('float32') / nmca)
            total = None
            for imca in range(nmca):
                dtcorr = self.dtfactor[:, imca].astype('float32')
                cor   = dtcorr.reshape((dtcorr.shape[0], 1))
                if total is None:
                    total = self.counts[:, imca, :] * cor
                else:
                    total = total + self.counts[:, imca, :] * cor

            self.total = total.astype('int16')
            self.dtfactor = self.dtfactor.astype('float32')
            self.dtfactor = self.dtfactor.transpose()
            self.inpcounts= self.inpcounts.transpose()
            self.outcounts= self.outcounts.transpose()
            self.livetime = self.livetime.transpose()
            self.realtime = self.realtime.transpose()
            self.counts   = self.counts.swapaxes(0, 1)

        self.read_ok = True