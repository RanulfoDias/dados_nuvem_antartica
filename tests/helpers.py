"""Synthetic spectra only; not an original instrument acquisition."""
def spe(detector=1, live=600, real=None, counts=(0, 0, 0, 0)):
    real = live if real is None else real
    return (f'$SPEC_ID:\nSynthetic detector {detector}\n$SPEC_REM:\nDET# {detector}\n'
            f'$DATE_MEA:\n10/14/2026 12:30:00\n$MEAS_TIM:\n{live} {real}\n'
            f'$DATA:\n0 {len(counts)-1}\n' + '\n'.join(map(str, counts)) +
            '\n$ROI:\n1\n0 3\n$ENER_FIT:\n0.0 1.0\n$MCA_CAL:\n2\n0.0 1.0 keV\n'
            '$SHAPE_CAL:\n3\n0.0 1.0 0.0\n').encode()
