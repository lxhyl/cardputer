"""SGP4 near-Earth satellite propagator.

Reference: Vallado, Crawford, Hujsak, Kelso (2006) "Revisiting Spacetrack
Report #3", AIAA 2006-6753.
https://celestrak.org/publications/AIAA/2006-6753/

Near-Earth branch only (period < 225 min): ISS ~92 min, Crew Dragon ~90 min,
Starlink ~96 min, etc.  Deep-space (SDP4) branch is NOT implemented here.

Internal units: Earth Radii (ER) for distance, minutes for time.
Output: positions in km, velocities in km/s, TEME reference frame.
"""
import math

# ---------------------------------------------------------------------------
# WGS-72 gravity constants — NORAD standard for TLE-format elements
# Source: Vallado et al. (2006), Table 1
# ---------------------------------------------------------------------------
_Re    = 6378.135           # km  Earth mean equatorial radius
_mu    = 398600.8           # km³/s²  gravitational parameter
_xke   = 60.0 * math.sqrt(_mu / _Re ** 3)   # ≈ 0.07436691613  ER^1.5/min
_J2    = 1.082616e-3
_J3    = -2.53881e-6
_J4    = -1.65597e-6
_J3OJ2 = _J3 / _J2
# "vkmpersec" velocity unit: 1 ER/TU expressed in km/s (TU = 1/xke min)
_vkmpersec = _xke * _Re / 60.0              # ≈ 7.905 km/s per velocity unit
_TWOPI     = 2.0 * math.pi
_DEG2RAD   = math.pi / 180.0
_RAD2DEG   = 180.0 / math.pi

# J2000.0 = 2000-01-01 12:00:00 UTC = Unix timestamp 946728000
_J2000_UNIX = 946728000.0


# ---------------------------------------------------------------------------
class SatRec:
    """Satellite record — orbital elements and precomputed SGP4 coefficients."""
    __slots__ = (
        'satnum', 'name',
        # Elements from TLE
        'inclo', 'nodeo', 'ecco', 'argpo', 'mo', 'no_kozai',
        'ndot', 'nddot', 'bstar', 'epoch_unix',
        # Derived by sgp4init
        'no', 'a', 'alta', 'altp',
        'method', 'isimp', 'error',
        # Drag and perturbation coefficients
        'cc1', 'cc4', 'cc5', 'D2', 'D3', 'D4',
        'eta', 'omgcof', 'xmcof', 'delmo', 'sinmao',
        'xlcof', 'aycof',
        # Geometry
        'cosio', 'sinio', 'cosio2',
        'x1mth2', 'x3thm1', 'x7thm1',
        # Secular drift rates
        'mdot', 'argpdot', 'nodedot', 'nodecf',
        't2cof', 't3cof', 't4cof', 't5cof',
        # GMST at epoch
        'gsto',
    )

    def __init__(self):
        self.error  = 0
        self.method = 'n'
        self.isimp  = False
        self.name   = ''
        self.satnum = ''


# ---------------------------------------------------------------------------
def tle_parse(name, line1, line2):
    """Parse 3-line TLE into a SatRec (elements set, NOT yet initialized).

    Call sgp4init() before calling sgp4().
    """
    s = SatRec()
    s.name = name.strip()

    def _packed(txt):
        """Decode packed exponential notation:  ±.NNNNN±E  →  float."""
        t = txt.strip()
        if not t:
            return 0.0
        sign = 1.0
        if t[0] == '-':
            sign = -1.0
            t = t[1:]
        elif t[0] == '+':
            t = t[1:]
        ep = -1
        for i in range(len(t) - 1, 0, -1):
            if t[i] in '+-':
                ep = i
                break
        if ep == -1:
            return sign * float('0.' + t) if t else 0.0
        esign = 1 if t[ep] == '+' else -1
        return sign * float('0.' + t[:ep]) * 10.0 ** (esign * int(t[ep + 1:]))

    def _epoch_to_unix(yr2, doy):
        yr2 = int(yr2)
        yr  = (2000 + yr2) if yr2 < 57 else (1900 + yr2)

        def _leap(y):
            return (y % 4 == 0 and y % 100 != 0) or (y % 400 == 0)

        days = 0
        if yr >= 1970:
            for y in range(1970, yr):
                days += 366 if _leap(y) else 365
        else:
            for y in range(yr, 1970):
                days -= 366 if _leap(y) else 365
        days += doy - 1.0   # day-of-year is 1-based
        return days * 86400.0

    s.satnum    = line1[2:7].strip()
    yr2         = int(line1[18:20])
    doy_frac    = float(line1[20:32])
    s.ndot      = float(line1[33:43]) * _TWOPI / (1440.0 ** 2)
    s.nddot     = _packed(line1[44:52]) * _TWOPI / (1440.0 ** 3)
    s.bstar     = _packed(line1[53:61])

    s.inclo     = float(line2[8:16])  * _DEG2RAD
    s.nodeo     = float(line2[17:25]) * _DEG2RAD
    s.ecco      = float('0.' + line2[26:33].strip())
    s.argpo     = float(line2[34:42]) * _DEG2RAD
    s.mo        = float(line2[43:51]) * _DEG2RAD
    s.no_kozai  = float(line2[52:63]) * _TWOPI / 1440.0  # rad/min

    s.epoch_unix = _epoch_to_unix(yr2, doy_frac)
    return s


# ---------------------------------------------------------------------------
def sgp4init(satrec):
    """Initialize SGP4 coefficients for satrec (in-place).

    Near-Earth branch: Vallado (2006) Section 3.
    Sets satrec.error=4 for deep-space satellites (period >= 225 min).
    """
    s     = satrec
    ecco  = s.ecco
    inclo = s.inclo
    no_k  = s.no_kozai

    # -- 1. Recover Brouwer mean motion from Kozai mean motion in TLE -------
    cosio  = math.cos(inclo)
    cosio2 = cosio * cosio
    omeosq = 1.0 - ecco * ecco          # 1 - e²
    rteosq = math.sqrt(omeosq)          # √(1 - e²)

    ak   = (_xke / no_k) ** (2.0 / 3.0)                      # initial a (ER)
    d1   = 0.75 * _J2 * (3.0 * cosio2 - 1.0) / (rteosq * omeosq)
    del1 = d1 / (ak * ak)
    ao   = ak * (1.0 - del1 * (1.0/3.0 + del1 * (1.0 + 134.0/81.0 * del1)))
    delo = d1 / (ao * ao)

    s.no   = no_k / (1.0 + delo)            # Brouwer mean motion (rad/min)
    s.a    = (_xke / s.no) ** (2.0 / 3.0)  # semi-major axis (ER)
    s.alta = s.a * (1.0 + ecco) - 1.0      # apogee height (ER)
    s.altp = s.a * (1.0 - ecco) - 1.0      # perigee height (ER)

    # Deep-space check (period >= 225 min → not handled)
    if _TWOPI / s.no >= 225.0:
        s.method = 'd'
        s.error  = 4
        return

    s.method = 'n'
    perigee  = s.altp * _Re    # km above surface

    # -- 2. Atmospheric layer parameters ------------------------------------
    if perigee < 156.0:
        s4 = max(20.0, perigee - 78.0)
    else:
        s4 = 42.0                   # standard: 42 km = 120-78 km
    s_r     = s4 / _Re + 1.0       # atmospheric reference height in ER
    qzms4t  = (s4 / _Re) ** 4      # (q₀ - s₀)⁴ in ER⁴

    s.isimp = (perigee < 220.0)

    # -- 3. Geometry constants ---------------------------------------------
    s.cosio  = cosio
    s.cosio2 = cosio2
    s.sinio  = math.sin(inclo)
    s.x1mth2 = 1.0 - cosio2          # sin²i
    s.x3thm1 = 3.0 * cosio2 - 1.0   # 3cos²i - 1
    s.x7thm1 = 7.0 * cosio2 - 1.0   # 7cos²i - 1

    a      = s.a
    e      = ecco
    p      = a * omeosq               # semi-latus rectum (ER)
    pinvsq = 1.0 / (p * p)

    # -- 4. Drag coefficients ----------------------------------------------
    tsi   = 1.0 / (a - s_r)
    s.eta = a * e * tsi
    eta   = s.eta
    etasq = eta * eta
    eeta  = e * eta
    psisq = abs(1.0 - etasq)
    if psisq < 1e-10:
        psisq = 1e-10

    coef  = qzms4t * tsi ** 4
    coef1 = coef / psisq ** 3.5

    cc2 = (coef1 * s.no
           * (a * (1.0 + 1.5 * etasq + eeta * (4.0 + etasq))
              + 0.75 * _J2 * tsi / psisq * s.x3thm1
              * (8.0 + 3.0 * etasq * (8.0 + etasq))))
    s.cc1 = s.bstar * cc2

    cc3 = 0.0
    if e > 1.0e-4:
        cc3 = -2.0 * coef * tsi * _J3OJ2 * s.no * s.sinio / e

    s.cc4 = (2.0 * s.no * coef1 * a * omeosq
             * (eta * (2.0 + 0.5 * etasq)
                + e * (0.5 + 2.0 * etasq)
                - _J2 * tsi / (a * psisq)
                * (-3.0 * s.x3thm1
                   * (1.0 - 2.0 * eeta + etasq * (1.5 - 0.5 * eeta))
                   + 0.75 * s.x1mth2
                   * (2.0 * etasq - eeta * (1.0 + etasq))
                   * math.cos(2.0 * s.argpo))))

    s.cc5 = 2.0 * coef1 * a * omeosq * (1.0 + 2.75 * (etasq + eeta) + eeta * etasq)

    # -- 5. Secular rates --------------------------------------------------
    cosio4 = cosio2 * cosio2
    temp1  = 1.5 * _J2 * pinvsq * s.no
    temp2  = 0.5 * temp1 * _J2 * pinvsq
    temp3  = -0.46875 * _J4 * pinvsq * pinvsq * s.no

    s.mdot    = (s.no
                 + 0.5 * temp1 * rteosq * s.x3thm1
                 + 0.0625 * temp2 * rteosq * (13.0 - 78.0 * cosio2 + 137.0 * cosio4))
    s.argpdot = (-0.5 * temp1 * (1.0 - 5.0 * cosio2)
                 + 0.0625 * temp2 * (7.0 - 114.0 * cosio2 + 395.0 * cosio4)
                 + temp3 * (3.0 - 36.0 * cosio2 + 49.0 * cosio4))
    xhdot1    = -temp1 * cosio
    s.nodedot = (xhdot1
                 + (0.5 * temp2 * (4.0 - 19.0 * cosio2)
                    + 2.0 * temp3 * (3.0 - 7.0 * cosio2)) * cosio)
    s.nodecf  = 3.5 * omeosq * xhdot1 * s.cc1
    s.t2cof   = 1.5 * s.cc1

    s.omgcof = s.bstar * cc3 * math.cos(s.argpo)
    if e > 1.0e-4:
        s.xmcof = -_TWOPI / 3.0 * coef * s.bstar / eeta
    else:
        s.xmcof = 0.0

    s.delmo  = (1.0 + s.eta * math.cos(s.mo)) ** 3
    s.sinmao = math.sin(s.mo)

    # J3 inclination correction for arg of latitude
    ci1p = cosio + 1.0
    if abs(ci1p) > 1.5e-12:
        s.xlcof = -0.25 * _J3OJ2 * s.sinio * (3.0 + 5.0 * cosio) / ci1p
    else:
        s.xlcof = -0.25 * _J3OJ2 * s.sinio * (3.0 + 5.0 * cosio) / 1.5e-12
    s.aycof = -0.5 * _J3OJ2 * s.sinio

    # Higher-order drag (skip for simplified model)
    if not s.isimp:
        cc1sq   = s.cc1 * s.cc1
        s.D2    = 4.0 * a * tsi * cc1sq
        tmp     = s.D2 * tsi * s.cc1 / 3.0
        s.D3    = (17.0 * a + s_r) * tmp
        s.D4    = 0.5 * tmp * a * tsi * (221.0 * a + 31.0 * s_r) * s.cc1
        s.t3cof = s.D2 + 2.0 * cc1sq
        s.t4cof = 0.25 * (3.0 * s.D3 + s.cc1 * (12.0 * s.D2 + 10.0 * cc1sq))
        s.t5cof = 0.2 * (3.0 * s.D4 + 12.0 * s.cc1 * s.D3
                         + 6.0 * s.D2 * s.D2
                         + 15.0 * cc1sq * (2.0 * s.D2 + cc1sq))
    else:
        s.D2 = s.D3 = s.D4 = 0.0
        s.t3cof = s.t4cof = s.t5cof = 0.0

    s.gsto  = _gmst_from_unix(s.epoch_unix)
    s.error = 0


# ---------------------------------------------------------------------------
def sgp4(satrec, tsince):
    """Propagate satrec tsince minutes from epoch.

    Returns (pos_km, vel_km_s) — both 3-tuples in TEME frame.
    Raises ValueError if underground or not initialized.
    """
    s = satrec
    if s.error:
        raise ValueError("sgp4init error {}".format(s.error))

    t  = tsince
    t2 = t * t

    # -- secular updates ---------------------------------------------------
    xmdf   = s.mo    + s.mdot    * t
    argpdf = s.argpo + s.argpdot * t
    nodedf = s.nodeo + s.nodedot * t

    argpm = argpdf
    mm    = xmdf

    if not s.isimp:
        delomg = s.omgcof * t
        delm   = s.xmcof * ((1.0 + s.eta * math.cos(xmdf)) ** 3 - s.delmo)
        mm     = xmdf   + delomg + delm
        argpm  = argpdf - delomg - delm
        t3     = t2 * t
        t4     = t3 * t
        tempa  = 1.0 - s.cc1 * t - s.D2 * t2 - s.D3 * t3 - s.D4 * t4
        tempe  = s.bstar * (s.cc4 * t + s.cc5 * (math.sin(mm) - s.sinmao))
        templ  = s.t2cof * t2 + s.t3cof * t3 + t4 * (s.t4cof + t * s.t5cof)
    else:
        tempa  = 1.0 - s.cc1 * t
        tempe  = s.bstar * s.cc4 * t
        templ  = s.t2cof * t2

    nodem  = (nodedf + s.nodecf * t2) % _TWOPI
    am     = ((_xke / s.no) ** (2.0 / 3.0)) * tempa * tempa
    nm     = _xke / am ** 1.5
    em     = s.ecco - tempe
    if em < 1.04e-3:
        em = 1.04e-3
    mm     = (mm + s.no * templ) % _TWOPI
    argpm  = argpm % _TWOPI
    xlm    = (mm + argpm + nodem) % _TWOPI
    mm     = (xlm - argpm - nodem) % _TWOPI

    # -- equinoctial Kepler equation (Newton-Raphson) ----------------------
    axnl = em * math.cos(argpm)
    tmp  = 1.0 / (am * (1.0 - em * em))
    aynl = em * math.sin(argpm) + tmp * s.aycof
    xl   = mm + argpm + nodem + tmp * s.xlcof * axnl

    u   = (xl - nodem) % _TWOPI
    eo1 = u
    for _ in range(10):
        sineo1 = math.sin(eo1)
        coseo1 = math.cos(eo1)
        denom  = 1.0 - coseo1 * axnl - sineo1 * aynl
        if abs(denom) < 1e-15:
            denom = 1e-15
        step = (u - aynl * coseo1 + axnl * sineo1 - eo1) / denom
        if   step >  0.95: step =  0.95
        elif step < -0.95: step = -0.95
        eo1 += step
        if abs(step) < 1e-12:
            break

    # -- short-period corrections ------------------------------------------
    ecose = axnl * math.cos(eo1) + aynl * math.sin(eo1)
    esine = axnl * math.sin(eo1) - aynl * math.cos(eo1)
    el2   = axnl * axnl + aynl * aynl
    pl    = am * (1.0 - el2)
    if pl < 0.0:
        raise ValueError("sgp4: pl<0 (underground)")

    rl     = am * (1.0 - ecose)
    rdotl  = math.sqrt(am) * esine / rl       # radial velocity (vel units)
    rvdotl = math.sqrt(pl) / rl               # transverse velocity (vel units)
    betal  = math.sqrt(1.0 - el2)
    tmp    = esine / (1.0 + betal)
    sinu   = am / rl * (math.sin(eo1) - aynl - axnl * tmp)
    cosu   = am / rl * (math.cos(eo1) - axnl + aynl * tmp)
    su     = math.atan2(sinu, cosu)
    sin2u  = (cosu + cosu) * sinu
    cos2u  = 1.0 - 2.0 * sinu * sinu
    tmp    = 1.0 / pl
    tmp1   = 0.5 * _J2 * tmp
    tmp2   = tmp1 * tmp

    # Radial, node, inclination corrections
    mrt   = rl * (1.0 - 1.5 * tmp2 * betal * s.x3thm1) + 0.5 * tmp1 * s.x1mth2 * cos2u
    su    = su - 0.25 * tmp2 * s.x7thm1 * sin2u
    xnode = nodem + 1.5 * tmp2 * s.cosio * sin2u
    xinc  = s.inclo + 1.5 * tmp2 * s.cosio * s.sinio * cos2u
    mvt   = rdotl  - nm * tmp1 * s.x1mth2 * sin2u / _xke
    rvdot = rvdotl + nm * tmp1 * (s.x1mth2 * cos2u + 1.5 * s.x3thm1) / _xke

    # -- ECI (TEME) position and velocity ----------------------------------
    sinsu = math.sin(su)
    cossu = math.cos(su)
    snod  = math.sin(xnode)
    cnod  = math.cos(xnode)
    sini  = math.sin(xinc)
    cosi  = math.cos(xinc)
    xmx   = -snod * cosi
    xmy   =  cnod * cosi
    ux    = xmx * sinsu + cnod * cossu
    uy    = xmy * sinsu + snod * cossu
    uz    = sini * sinsu
    vx    = xmx * cossu - cnod * sinsu
    vy    = xmy * cossu - snod * sinsu
    vz    = sini * cossu

    # Position: ER → km;  Velocity: vel_units → km/s
    r_km = mrt * _Re
    pos  = (r_km * ux, r_km * uy, r_km * uz)
    vel  = ((mvt * ux + rvdot * vx) * _vkmpersec,
            (mvt * uy + rvdot * vy) * _vkmpersec,
            (mvt * uz + rvdot * vz) * _vkmpersec)
    return pos, vel


# ---------------------------------------------------------------------------
def _gmst_from_unix(unix_t):
    """Greenwich Mean Sidereal Time (radians) from UTC Unix timestamp.

    IAU 1982 formula — accurate to ~0.01° over ±50 years from J2000.
    """
    du = (unix_t - _J2000_UNIX) / 86400.0
    return ((280.46061837 + 360.98564736629 * du) % 360.0) * _DEG2RAD


def teme_to_azel(pos_teme_km, lat_deg, lon_deg, unix_t):
    """Convert satellite TEME position to topocentric Az/El/Range.

    pos_teme_km : (x, y, z) in km, TEME frame (output of sgp4)
    lat_deg     : observer geodetic latitude, degrees
    lon_deg     : observer longitude, degrees
    unix_t      : UTC Unix timestamp

    Returns (az_deg, el_deg, range_km)
      az_deg   : azimuth 0–360° clockwise from North
      el_deg   : elevation above horizon (negative = below horizon)
      range_km : slant range in km
    """
    theta = _gmst_from_unix(unix_t)
    st = math.sin(theta)
    ct = math.cos(theta)

    # TEME → ECEF:  rotate by GMST about z-axis
    px, py, pz = pos_teme_km
    xe =  px * ct + py * st
    ye = -px * st + py * ct
    ze =  pz

    # Observer ECEF (spherical Earth; oblateness adds <0.5° error at poles)
    lat  = lat_deg * _DEG2RAD
    lon  = lon_deg * _DEG2RAD
    slat = math.sin(lat)
    clat = math.cos(lat)
    slon = math.sin(lon)
    clon = math.cos(lon)
    ox   = _Re * clat * clon
    oy   = _Re * clat * slon
    oz   = _Re * slat

    # Range vector in ECEF
    dx = xe - ox
    dy = ye - oy
    dz = ze - oz

    # East-North-Up components
    e  =  -slon * dx + clon * dy
    n  =  -slat * clon * dx - slat * slon * dy + clat * dz
    up =   clat * clon * dx + clat * slon * dy + slat * dz

    rng = math.sqrt(dx * dx + dy * dy + dz * dz)
    if rng < 1.0:
        return 0.0, 90.0, rng

    el = math.asin(max(-1.0, min(1.0, up / rng))) * _RAD2DEG
    az = (math.atan2(e, n) * _RAD2DEG) % 360.0
    return az, el, rng
