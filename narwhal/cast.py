# -*- coding: utf-8 -*-
"""
Cast and CastCollection classes for managing CTD observations
"""

import os
import sys
import abc
import collections
import itertools
import json
import gzip
import copy
from functools import reduce
import six
import numpy as np
from scipy import ndimage
from scipy import sparse as sprs
from scipy.interpolate import UnivariateSpline
from scipy.io import netcdf_file
from karta import Point, Multipoint
from . import fileio
from . import gsw
from . import util

try:
    from karta.crs import crsreg
except ImportError:
    import karta as crsreg
LONLAT = crsreg.LONLAT
CARTESIAN = crsreg.CARTESIAN

# Global physical constants
G = 9.8
OMEGA = 2*np.pi / 86400.0


class Cast(object):
    """ A Cast is a set of referenced measurements associated with a single
    coordinate.

    Vector water properties are provided as keyword arguments. There are
    several reserved keywords:

    coords::iterable[2]     the geographic coordinates of the observation

    properties::dict        scalar metadata

    primarykey::string      the name of vertical measure. Usually pressure
                            ("pres"), but could be e.g. depth ("z")
    """

    _type = "cast"

    def __init__(self, p, coords=None, properties=None, primarykey="pres",
                 **kwargs):

        if properties is None:
            self.properties = {}
        elif isinstance(properties, dict):
            self.properties = properties
        else:
            raise TypeError("properties must be a dictionary")
        self.properties["coordinates"] = coords

        self.primarykey = primarykey
        self.data = dict()
        self.data[primarykey] = np.asarray(p)

        # Python 3 workaround
        try:
            items = kwargs.iteritems()
        except AttributeError:
            items = kwargs.items()

        # Populate vector and scalar data fields
        self._fields = [primarykey]
        for (kw, val) in items:
            if isinstance(val, collections.Container) and \
                    not isinstance(val, str) and \
                    len(val) == len(p):
                self.data[kw] = np.asarray(val)
                self._fields.append(kw)
            else:
                self.properties[kw] = val

        self._len = len(p)
        return

    def __len__(self):
        return self._len

    def __str__(self):
        if self.coords is not None:
            coords = tuple(round(c, 3) for c in self.coords)
        else:
            coords = (None, None)
        s = "CTD cast (" + "".join([str(k)+", " for k in self._fields])
        # cut off the final comma
        s = s[:-2] + ") at {0}".format(coords)
        return s

    def __getitem__(self, key):
        if isinstance(key, int):
            if key < self._len:
                return tuple((a, self.data[a][key]) for a in self._fields
                             if hasattr(self.data[a], "__iter__"))
            else:
                raise IndexError("Index ({0}) is greater than cast length "
                                 "({1})".format(key, self._len))
        elif key in self.data:
            return self.data[key]
        elif key in self.properties:
            return self.properties[key]
        else:
            raise KeyError("No item {0}".format(key))
        return

    def __setitem__(self, key, val):
        if isinstance(key, str):
            if isinstance(val, collections.Container) and \
                    not isinstance(val, str) and \
                    len(val) == len(self[self.primarykey]):
                self.data[key] = val
                if key not in self._fields:
                    self._fields.append(key)
            else:
                self.properties[key] = val

        elif isinstance(key, int):
            raise KeyError("Cast object profiles are not mutable")
        else:
            raise KeyError("Cannot use {0} as a hash".format(key))
        return

    def __add__(self, other):
        if isinstance(other, AbstractCast):
            return CastCollection(self, other)
        elif isinstance(other, AbstractCastCollection):
            return CastCollection(self, *[a for a in other])
        else:
            raise TypeError("No rule to add {0} to {1}".format(type(self),
                                                               type(other)))

    def __eq__(self, other):
        if self._fields != other._fields or \
                self.properties != other.properties or \
                self.coords != other.coords or \
                any(np.any(self.data[k] != other.data[k]) for k in self._fields):
            return False
        else:
            return True

    def __ne__(self, other):
        return not self.__eq__(other)

    def _addkeydata(self, key, data, overwrite=False):
        """ Add `data::array` under `key::string`. If `key` already exists,
        iterates over [key]_2, [key]_3... until an unused identifier is found.
        Returns the key finally used.

        Use case: for automatic addition of fields.
        """
        key_ = key
        if not overwrite:
            i = 2
            while key_ in self.data:
                key_ = key + "_" + str(i)
                i += 1
        if key_ not in self._fields:
            self._fields.append(key_)
        self.data[key_] = data
        return key_

    @property
    def fields(self):
        return self._fields

    @property
    def coords(self):
        return self.properties["coordinates"]

    def nanmask(self, fields=None):
        """ Return a mask for observations containing at least one NaN. """
        if fields is None:
            fields = self._fields
        vectors = [v for (k,v) in self.data.items() if k in fields]
        return np.isnan(np.vstack(vectors).sum(axis=0))

    def nvalid(self, fields=None):
        """ Return the number of complete (non-NaN) observations. """
        if fields is None:
            fields = self._fields
        elif isinstance(fields, str):
            fields = (fields,)
        vectors = [self.data[k] for k in fields]
        if len(vectors) == 1:
            nv = sum(~np.isnan(vectors[0]))
        else:
            nv = sum(reduce(lambda a,b: (~np.isnan(a))&(~np.isnan(b)), vectors))
        return nv

    def extend(self, n, padvalue=np.nan):
        """ Add `n::int` NaN depth levels to cast. """
        if n == 0:
            raise ValueError("Cast already has length {0}".format(n))
        elif n < 0:
            raise ValueError("Cast is longer than length {0}".format(n))
        else:
            empty_array = padvalue * np.empty(n, dtype=np.float64)
        for key in self.data:
            arr = np.hstack([self.data[key], empty_array])
            self.data[key] = arr
        self._len = len(self) + n
        return

    def interpolate(self, y, x, v, force=False):
        """ Interpolate property y as a function of property x at values given
        by vector x=v.

        y::string       name of property to interpolate

        x::string       name of reference property

        v::iterable     vector of values for x

        force::bool     whether to coerce x to be monotonic (defualt False)

        Note: it's difficult to interpolate when x is not monotic, because this
        makes y not a true function. However, it's resonable to want to
        interpolate using rho or sigma as x. These should be essentially
        monotonic, but might not be due to measurement noise. The keyword
        argument `force` can be provided as True, which causes nonmonotonic x
        to be coerced into a monotonic form (see `force_monotonic`).
        """
        if y not in self.data:
            raise KeyError("Cast has no property '{0}'".format(y))
        elif x not in self.data:
            raise KeyError("Cast has no property '{0}'".format(x))
        if np.all(np.diff(self[x]) > 0.0):
            return np.interp(v, self[x], self[y])
        elif force:
            return np.interp(v, util.force_monotonic(self[x]), self[y])
        else:
            raise ValueError("x is not monotonic")

    def regrid(self, levels):
        """ Re-interpolate Cast at specified grid levels. Returns a new Cast. """
        # some low level voodoo
        ret = copy.deepcopy(self)
        ret._len = len(levels)
        for key in self.data:
            if key is not self.primarykey:
                ret.data[key] = np.interp(levels, self[self.primarykey], self[key],
                                          left=np.nan, right=np.nan)
        ret.data[self.primarykey] = levels
        return ret

    def save(self, fnm, binary=True):
        """ Save a JSON-formatted representation to a file at `fnm::string`.
        """
        if hasattr(fnm, "write"):
            fileio.writecast(fnm, self, binary=binary)
        else:
            if binary:
                if os.path.splitext(fnm)[1] != ".nwz":
                    fnm = fnm + ".nwz"
                with gzip.open(fnm, "wb") as f:
                    fileio.writecast(f, self, binary=True)
            else:
                if os.path.splitext(fnm)[1] != ".nwl":
                    fnm = fnm + ".nwl"
                with open(fnm, "w") as f:
                    fileio.writecast(f, self, binary=False)
        return


class CTDCast(Cast):
    """ Specialization of Cast guaranteed to have salinity and temperature
    fields. """
    _type = "ctdcast"

    def __init__(self, p, sal, temp, coords=None, properties=None,
                 **kwargs):
        super(CTDCast, self).__init__(p, sal=sal, temp=temp, coords=coords,
                                      properties=properties, **kwargs)
        return

    def add_density(self):
        """ Add in-situ density to fields, and return the field name. """
        SA = gsw.sa_from_sp(self["sal"], self["pres"],
                            [self.coords[0] for _ in self["sal"]],
                            [self.coords[1] for _ in self["sal"]])
        CT = gsw.ct_from_t(SA, self["temp"], self["pres"])
        rho = gsw.rho(SA, CT, self["pres"])
        return self._addkeydata("rho", np.asarray(rho))

    def add_depth(self, rhokey=None):
        """ Use temperature, salinity, and pressure to calculate depth. If
        in-situ density is already in a field, `rhokey::string` can be provided to
        avoid recalculating it. """
        if rhokey is None:
            rhokey = self.add_density()
        rho = self[rhokey]

        # remove initial NaNs by replacing them with the first non-NaN
        nnans = 0
        r = rho[0]
        while np.isnan(r):
            nnans += 1
            r = rho[nnans]
        rho[:nnans] = rho[nnans+1]

        dp = np.hstack([self["pres"][0], np.diff(self["pres"])])
        dz = dp / (rho * G) * 1e4
        depth = np.cumsum(dz)
        return self._addkeydata("depth", depth)

    def add_Nsquared(self, rhokey=None, s=0.2):
        """ Calculate the squared buoyancy frequency, based on density given by
        `rhokey::string`. Uses a smoothing spline with smoothing factor
        `s::float` (smaller values of `s` give a noisier result). """
        if rhokey is None:
            rhokey = self.add_density()
        msk = self.nanmask((rhokey, "pres"))
        rho = self[rhokey][~msk]
        pres = self["pres"][~msk]
        rhospl = UnivariateSpline(pres, rho, s=s)
        drhodz = np.asarray([-rhospl.derivatives(p)[1] for p in pres])
        N2 = np.empty(len(self), dtype=np.float64)
        N2[msk] = np.nan
        N2[~msk] = -G / rho * drhodz
        return self._addkeydata("N2", N2)

    def baroclinic_modes(self, nmodes, ztop=10):
        """ Calculate the baroclinic normal modes based on linear
        quasigeostrophy and the vertical stratification. Return the first
        `nmodes::int` deformation radii and their associated eigenfunctions.

        Additional arguments
        --------------------

        ztop            the depth at which to cut off the profile, to avoid
                        surface effects
        """
        if "N2" not in self.fields:
            self.add_Nsquared()
        if "depth" not in self.fields:
            self.add_depth()

        igood = ~self.nanmask(("N2", "depth"))
        N2 = self["N2"][igood]
        dep = self["depth"][igood]

        itop = np.argwhere(dep > ztop)[0]
        N2 = N2[itop:]
        dep = dep[itop:]

        h = np.diff(dep)
        assert all(h == h_ for h_ in h[1:])     # requires uniform gridding for now

        f = 4*OMEGA * math.sin(self.coords[1])
        F = f**2/N2
        F[0] = 0.0
        F[-1] = 0.0
        F = sprs.diags(F, 0)

        D1 = util.sparse_diffmat(len(self), 1, h)
        D2 = util.sparse_diffmat(len(self), 2, h)

        T = sparse.diags(D1 * F.diagonal(), 0)
        M = T*D1 + F*D2
        lamda, V = sprs.linalg.eigs(M.tocsc(), k=nmodes+1, sigma=1e-8)
        Ld = 1.0 / np.sqrt(np.abs(np.real(lamda[1:])))
        return Ld, V[:,1:]

    def water_fractions(self, sources, tracers=("sal", "temp")):
        """ Compute water mass fractions based on conservative tracers.
        `sources::[tuple, tuple, ...]` is a list of tuples giving the prototype water
        masses.

        tracers::[string, string]       must be fields in the CTDCast to use as
                                        conservative tracers
                                        [default: ("sal", "temp")].
        """

        if len(sources) != 3:
            raise ValueError("Three potential source waters must be given "
                             "(not {0})".format(len(sources)))
        n = self.nvalid(tracers)
        I = sprs.eye(n)
        A_ = np.array([[sources[0][0], sources[1][0], sources[2][0]],
                       [sources[0][1], sources[1][1], sources[2][1]],
                       [         1.0,          1.0,          1.0]])
        As = sprs.kron(I, A_, "csr")
        b = np.empty(3*n)
        msk = self.nanmask(tracers)
        b[::3] = self[tracers[0]][~msk]
        b[1::3] = self[tracers[1]][~msk]
        b[2::3] = 1.0               # lagrange multiplier

        frac = sprs.linalg.spsolve(As, b)
        mass1 = np.empty(len(self)) * np.nan
        mass2 = np.empty(len(self)) * np.nan
        mass3 = np.empty(len(self)) * np.nan
        mass1[~msk] = frac[::3]
        mass2[~msk] = frac[1::3]
        mass3[~msk] = frac[2::3]
        return (mass1, mass2, mass3)


class LADCP(Cast):
    """ Specialization of Cast for LADCP data. Requires *u* and *v* fields. """
    _type = "ladcpcast"

    def __init__(self, z, u, v, coords=None, properties=None,
                 primarykey="z", **kwargs):
        super(LADCP, self).__init__(z, u=u, v=v, coords=coords,
                                    properties=properties, primarykey=primarykey,
                                    **kwargs)
        return

    def add_shear(self, sigma=None):
        """ Compute the velocity shear for *u* and *v*. If *sigma* is not None,
        smooth the data with a gaussian filter before computing the derivative.
        """
        if sigma is not None:
            u = ndimage.filters.gaussian_filter1d(self["u"], sigma)
            v = ndimage.filters.gaussian_filter1d(self["v"], sigma)
        else:
            u = self["u"]
            v = self["v"]

        dudz = util.diff1(u, self["z"])
        dvdz = util.diff1(v, self["z"])
        self._addkeydata("dudz", dudz)
        self._addkeydata("dvdz", dvdz)
        return


class XBTCast(Cast):
    """ Specialization of Cast with temperature field. """
    _type = "xbtcast"

    def __init__(self, z, temp, coords=None, properties=None,
                 primarykey="z", **kwargs):
        super(XBTCast, self).__init__(z, temp=temp, coords=coords,
                                      properties=properties, primarykey=primarykey,
                                      **kwargs)
        return


class CastCollection(collections.Sequence):
    """ A CastCollection is an indexable collection of Cast instances.

    Create from casts or an iterable ordered sequence of casts:

        CastCollection(cast1, cast2, cast3...)

    or

        CastCollection([cast1, cast2, cast3...])
    """
    _type = "castcollection"

    def __init__(self, *args):
        if len(args) == 0:
            self.casts = []
        elif isinstance(args[0], Cast):
            self.casts = list(args)
        elif (len(args) == 1) and all(isinstance(a, Cast) for a in args[0]):
            self.casts = args[0]
        else:
            raise TypeError("Arguments must be either Cast types or an "
                            "iterable collection of Cast types")
        return

    def __len__(self):
        return len(self.casts)

    def __getitem__(self, key):
        if isinstance(key, int):
            return self.casts.__getitem__(key)
        elif isinstance(key, slice):
            return type(self)(self.casts.__getitem__(key))
        elif all(key in cast.data for cast in self.casts):
            return np.vstack([a[key] for a in self.casts]).T
        elif all(key in cast.properties for cast in self.casts):
            return [cast.properties[key] for cast in self.casts]
        else:
            raise KeyError("Key {0} not found in all casts".format(key))

    def __eq__(self, other):
        if not isinstance(self, type(other)):
            return False
        if len(self) != len(other):
            return False
        for (ca, cb) in zip(self, other):
            if ca != cb:
                return False
        return True

    def __ne__(self, other):
        return not self.__eq__(other)

    def __contains__(self, cast):
        return True if (cast in self.casts) else False

    def __iter__(self):
        return (a for a in self.casts)

    def __add__(self, other):
        if isinstance(other, AbstractCastCollection):
            return CastCollection(list(a for a in itertools.chain(self.casts, other.casts)))
        elif isinstance(other, AbstractCast):
            return CastCollection(self.casts + [other])
        else:
            raise TypeError("Can only add castcollection and *cast types to "
                            "CastCollection")

    def __repr__(self):
        s = "CastCollection with {n} casts:".format(n=len(self.casts))
        i = 0
        while i != 10 and i != len(self.casts):
            c = self[i]
            s +=  ("\n  {num:3g} {typestr:6s} {lon:3.3f} {lat:2.3f}    "
                    "{keys}".format(typestr=c._type[:-4], num=i+1,
                                    lon=c.coords[0], lat=c.coords[1],
                                    keys=c._fields[:8]))
            if len(c._fields) > 8:
                s += " ..."
            i += 1
        if len(self.casts) > 10:
            s += "\n  (...)"
        return s

    @property
    def coords(self):
        return Multipoint([c.coords for c in self], crs=LONLAT)

    def add_bathymetry(self, bathymetry):
        """ Reference Bathymetry instance `bathymetry` to CastCollection.

        bathymetry::Bathymetry2d        bathymetry instance
        """
        for cast in self.casts:
            if hasattr(cast, "coords"):
                cast.properties["depth"] = bathymetry.atxy(*cast.coords)
            else:
                cast.properties["tdepth"] = np.nan
                sys.stderr.write("Warning: cast has no coordinates")
        return

    def mean(self):
        raise NotImplementedError()

    def castwhere(self, key, value):
        """ Return the first cast where cast.properties[key] == value """
        for cast in self.casts:
            if cast.properties.get(key, None) == value:
                return cast

    def castswhere(self, key, values):
        """ Return all casts with a property key that is in `values::Container`
        """
        if not isinstance(values, collections.Container) or isinstance(values, str):
            values = (values,)
        casts = []
        for cast in self.casts:
            if cast.properties.get(key, None) in values:
                casts.append(cast)
        return CastCollection(casts)

    def defray(self, padvalue=np.nan):
        """ Pad casts to all have the same length, and return a copy.
        
        Warning: does not correct differing pressure bins, which require
        explicit interpolation.
        """
        n = max(len(c) for c in self)
        casts = []
        for cast_ in self:
            cast = copy.deepcopy(cast_)
            if len(cast) < n:
                dif = n - len(cast)
                cast.extend(dif, padvalue=padvalue)
                casts.append(cast)
            else:
                casts.append(cast)
        return CastCollection(casts)

    def asarray(self, key):
        """ Naively return values as an array, assuming that all casts are indexed
        with the same pressure levels.

        key::string         property to return
        """
        nrows = max(cast._len for cast in self.casts)
        arr = np.nan * np.empty((nrows, len(self.casts)), dtype=np.float64)
        for i, cast in enumerate(self.casts):
            arr[:cast._len, i] = cast[key]
        return arr

    def projdist(self):
        """ Return the cumulative distances from the cast to cast.
        """
        cumulative = [0]
        a = Point(self.casts[0].coords, crs=LONLAT)
        for cast in self.casts[1:]:
            b = Point(cast.coords, crs=LONLAT)
            cumulative.append(cumulative[-1] + a.distance(b))
            a = b
        return np.asarray(cumulative, dtype=np.float64)

    def thermal_wind(self, tempkey="temp", salkey="sal", rhokey=None,
                     dudzkey="dudz", ukey="u", overwrite=False):
        """ Compute profile-orthagonal velocity shear using hydrostatic thermal
        wind. In-situ density is computed from temperature and salinity unless
        *rhokey* is provided.

        Adds a U field and a ∂U/∂z field to each cast in the collection. As a
        side-effect, if casts have no "depth" field, one is added and populated
        from temperature and salinity fields.

        Parameters
        ----------

        tempkey::string     key to use for temperature if *rhokey* is None

        salkey::string      key to use for salinity if *rhokey* is None

        rhokey::string      key to use for density, or None [default: None]

        dudzkey::string     key to use for ∂U/∂z, subject to *overwrite*

        ukey::string        key to use for U, subject to *overwrite*

        overwrite::bool     whether to allow cast fields to be overwritten
                            if False, then *ukey* and *dudzkey* are incremented
                            until there is no clash
        """
        if rhokey is None:
            rhokeys = []
            for cast in self.casts:
                rhokeys.append(cast.add_density())
            if any(r != rhokeys[0] for r in rhokeys[1:]):
                raise NameError("Tried to add density field, but ended up with "
                                "different keys - aborting")
            else:
                rhokey = rhokeys[0]

        rho = self.asarray(rhokey)
        (m, n) = rho.shape

        for cast in self:
            if "depth" not in cast.data.keys():
                cast.add_depth()

        drho = util.diff2_dinterp(rho, self.projdist())
        sinphi = np.sin([c.coords[1]*np.pi/180.0 for c in self.casts])
        dudz = (G / rho * drho) / (2*OMEGA*sinphi)
        u = util.uintegrate(dudz, self.asarray("depth"))

        for (ic,cast) in enumerate(self.casts):
            cast._addkeydata(dudzkey, dudz[:,ic], overwrite=overwrite)
            cast._addkeydata(ukey, u[:,ic], overwrite=overwrite)
        return

    def thermal_wind_inner(self, tempkey="temp", salkey="sal", rhokey=None,
                           dudzkey="dudz", ukey="u", bottomkey="depth",
                           overwrite=False):
        """ Alternative implementation that creates a new cast collection
        consistng of points between the observation casts.

        Compute profile-orthagonal velocity shear using hydrostatic thermal
        wind. In-situ density is computed from temperature and salinity unless
        *rhokey* is provided.

        Adds a U field and a ∂U/∂z field to each cast in the collection. As a
        side-effect, if casts have no "depth" field, one is added and populated
        from temperature and salinity fields.

        Parameters
        ----------

        tempkey::string     key to use for temperature if *rhokey* is None

        salkey::string      key to use for salinity if *rhokey* is None

        rhokey::string      key to use for density, or None [default: None]

        dudzkey::string     key to use for ∂U/∂z, subject to *overwrite*

        ukey::string        key to use for U, subject to *overwrite*

        overwrite::bool     whether to allow cast fields to be overwritten
                            if False, then *ukey* and *dudzkey* are incremented
                            until there is no clash
        """
        if rhokey is None:
            rhokeys = []
            for cast in self.casts:
                rhokeys.append(cast.add_density())
            if any(r != rhokeys[0] for r in rhokeys[1:]):
                raise NameError("Tried to add density field, but ended up with "
                                "different keys - aborting")
            else:
                rhokey = rhokeys[0]

        rho = self.asarray(rhokey)
        (m, n) = rho.shape

        def avgcolumns(a, b):
            avg = a if len(a[~np.isnan(a)]) > len(b[~np.isnan(b)]) else b
            return avg

        # Add casts in intermediate positions
        midcasts = []
        for i in range(len(self.casts)-1):
            c1 = self[i].coords
            c2 = self[i+1].coords
            cmid = (0.5*(c1[0]+c2[0]), 0.5*(c1[1]+c2[1]))
            p = avgcolumns(self[i]["pres"], self[i+1]["pres"])
            t = avgcolumns(self[i]["temp"], self[i+1]["temp"])
            s = avgcolumns(self[i]["sal"], self[i+1]["sal"])
            cast = CTDCast(p, temp=t, sal=s, primarykey="pres", coords=cmid)
            cast.add_depth()
            cast.properties[bottomkey] = 0.5 * (self[i].properties[bottomkey] +
                                                self[i+1].properties[bottomkey])
            midcasts.append(cast)

        coll = CastCollection(midcasts)
        drho = util.diff2_inner(rho, self.projdist())
        sinphi = np.sin([c.coords[1]*np.pi/180.0 for c in midcasts])
        rhoavg = 0.5 * (rho[:,:-1] + rho[:,1:])
        dudz = (G / rhoavg * drho) / (2*OMEGA*sinphi)
        u = util.uintegrate(dudz, coll.asarray("depth"))

        for (ic,cast) in enumerate(coll):
            cast._addkeydata(dudzkey, dudz[:,ic], overwrite=overwrite)
            cast._addkeydata(ukey, u[:,ic], overwrite=overwrite)
        return coll

    def save(self, fnm, binary=True):
        """ Save a JSON-formatted representation to a file.

        fnm::string     File name to save to
        """
        if hasattr(fnm, "write"):
            fileio.writecastcollection(fnm, self, binary=binary)
        else:
            if binary:
                if os.path.splitext(fnm)[1] != ".nwz":
                    fnm = fnm + ".nwz"
                with gzip.open(fnm, "wb") as f:
                    fileio.writecastcollection(f, self, binary=True)
            else:
                if os.path.splitext(fnm)[1] != ".nwl":
                    fnm = fnm + ".nwl"
                with open(fnm, "w") as f:
                    fileio.writecastcollection(f, self, binary=False)
        return


def read(fnm):
    """ Convenience function for reading JSON-formatted measurement data from
    `fnm::string`.
    """
    try:
        with open(fnm, "r") as f:
            d = json.load(f)
    except (UnicodeDecodeError,ValueError) as e:
        with gzip.open(fnm, "rb") as f:
            s = f.read().decode("utf-8")
            d = json.loads(s)
    return _fromjson(d)

def _fromjson(d):
    """ Lower level function to (possibly recursively) convert JSON into
    narwhal object. """
    typ = d.get("type", None)
    if typ == "cast":
        return fileio.dictascast(d, Cast)
    elif typ == "ctdcast":
        return fileio.dictascast(d, CTDCast)
    elif typ == "xbtcast":
        return fileio.dictascast(d, XBTCast)
    elif typ == "ladcpcast":
        return fileio.dictascast(d, LADCP)
    elif typ == "castcollection":
        casts = [_fromjson(castdict) for castdict in d["casts"]]
        return CastCollection(casts)
    elif typ is None:
        raise AttributeError("couldn't read data type - file may be corrupt")
    else:
        raise LookupError("Invalid type: {0}".format(typ))

def read_woce_netcdf(fnm):
    """ Read a CTD cast from a WOCE NetCDF file. """

    def getvariable(nc, key):
        return nc.variables[key].data.copy()

    nc = netcdf_file(fnm)
    coords = (getvariable(nc, "longitude")[0], getvariable(nc, "latitude")[0])

    pres = getvariable(nc, "pressure")
    sal = getvariable(nc, "salinity")
    salqc = getvariable(nc, "salinity_QC")
    sal[salqc!=2] = np.nan
    temp = getvariable(nc, "temperature")
    # tempqc = getvariable(nc, "temperature_QC")
    # temp[tempqc!=2] = np.nan
    oxy = getvariable(nc, "oxygen")
    oxyqc = getvariable(nc, "oxygen_QC")
    oxy[oxyqc!=2] = np.nan

    date = getvariable(nc, "woce_date")
    time = getvariable(nc, "woce_time")
    return narwhal.CTDCast(pres, sal, temp, oxygen=oxy,
                           coords=coords,
                           properties={"woce_time":time, "woce_date":date})

class AbstractCast(six.with_metaclass(abc.ABCMeta)):
    pass

class AbstractCastCollection(six.with_metaclass(abc.ABCMeta)):
    pass

AbstractCast.register(Cast)
AbstractCast.register(CTDCast)
AbstractCast.register(XBTCast)
AbstractCast.register(LADCP)
AbstractCastCollection.register(CastCollection)

