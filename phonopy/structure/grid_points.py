# Copyright (C) 2014 Atsushi Togo
# All rights reserved.
#
# This file is part of phonopy.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions
# are met:
#
# * Redistributions of source code must retain the above copyright
#   notice, this list of conditions and the following disclaimer.
#
# * Redistributions in binary form must reproduce the above copyright
#   notice, this list of conditions and the following disclaimer in
#   the documentation and/or other materials provided with the
#   distribution.
#
# * Neither the name of the phonopy project nor the names of its
#   contributors may be used to endorse or promote products derived
#   from this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
# "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
# LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS
# FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE
# COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT,
# INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING,
# BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
# LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
# CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT
# LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN
# ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.

import warnings
import numpy as np
from spglib import (
    get_stabilized_reciprocal_mesh, relocate_BZ_grid_address,
    get_symmetry_dataset, get_pointgroup)
from phonopy.structure.brillouin_zone import get_qpoints_in_Brillouin_zone
from phonopy.structure.symmetry import get_lattice_vector_equivalence
from phonopy.structure.cells import (
    get_primitive_matrix_by_centring, estimate_supercell_matrix,
    estimate_supercell_matrix_from_pointgroup)
from phonopy.structure.snf import SNF3x3


def length2mesh(length, lattice, rotations=None):
    """Convert length to mesh for q-point sampling

    This conversion for each reciprocal axis follows VASP convention by
        N = max(1, int(l * |a|^* + 0.5))
    'int' means rounding down, not rounding to nearest integer.

    Parameters
    ----------
    length : float
        Length having the unit of direct space length.
    lattice : array_like
        Basis vectors of primitive cell in row vectors.
        dtype='double', shape=(3, 3)
    rotations: array_like, optional
        Rotation matrices in real space. When given, mesh numbers that are
        symmetrically reasonable are returned. Default is None.
        dtype='intc', shape=(rotations, 3, 3)

    Returns
    -------
    array_like
        dtype=int, shape=(3,)

    """

    rec_lattice = np.linalg.inv(lattice)
    rec_lat_lengths = np.sqrt(np.diagonal(np.dot(rec_lattice.T, rec_lattice)))
    mesh_numbers = np.rint(rec_lat_lengths * length).astype(int)

    if rotations is not None:
        reclat_equiv = get_lattice_vector_equivalence(
            [r.T for r in np.array(rotations)])
        m = mesh_numbers
        mesh_equiv = [m[1] == m[2], m[2] == m[0], m[0] == m[1]]
        for i, pair in enumerate(([1, 2], [2, 0], [0, 1])):
            if reclat_equiv[i] and not mesh_equiv:
                m[pair] = max(m[pair])

    return np.maximum(mesh_numbers, [1, 1, 1])


def get_qpoints(mesh_numbers,
                reciprocal_lattice,  # column vectors
                q_mesh_shift=None,  # Monkhorst-Pack style grid shift
                is_gamma_center=True,
                is_time_reversal=True,
                fit_in_BZ=True,
                rotations=None,  # Point group operations in real space
                is_mesh_symmetry=True):
    gp = GridPoints(mesh_numbers,
                    reciprocal_lattice,
                    q_mesh_shift=q_mesh_shift,
                    is_gamma_center=is_gamma_center,
                    is_time_reversal=is_time_reversal,
                    fit_in_BZ=fit_in_BZ,
                    rotations=rotations,
                    is_mesh_symmetry=is_mesh_symmetry)

    return gp.qpoints, gp.weights


def extract_ir_grid_points(grid_mapping_table):
    ir_grid_points = np.array(np.unique(grid_mapping_table),
                              dtype=grid_mapping_table.dtype)
    weights = np.zeros_like(grid_mapping_table)
    for i, gp in enumerate(grid_mapping_table):
        weights[gp] += 1
    ir_weights = np.array(weights[ir_grid_points], dtype='intc')

    return ir_grid_points, ir_weights


class GridPoints(object):
    """Class to generate irreducible grid points on uniform mesh grids

    Attributes
    ----------
    mesh_numbers: ndarray
       Mesh numbers along a, b, c axes.
       dtype='intc'
       shape=(3,)
    reciprocal_lattice: array_like
        Basis vectors in reciprocal space. a*, b*, c* are given in column
        vectors.
        dtype='double'
        shape=(3, 3)
    qpoints: ndarray
       q-points in reduced coordinates of reciprocal lattice
       dtype='double'
       shape=(ir-grid points, 3)
    weights: ndarray
       Geometric q-point weights. Its sum is the number of grid points.
       dtype='intc'
       shape=(ir-grid points,)
    grid_address: ndarray
       Addresses of all grid points represented by integers.
       dtype='intc'
       shape=(prod(mesh_numbers), 3)
    ir_grid_points: ndarray
        Indices of irreducible grid points in grid_address.
        dtype='uintp', shape=(ir-grid points,)
    grid_mapping_table: ndarray
        Index mapping table from all grid points to ir-grid points.
        dtype='uintp', shape=(prod(mesh_numbers),)

    """

    def __init__(self,
                 mesh_numbers,
                 reciprocal_lattice,
                 q_mesh_shift=None,  # Monkhorst-Pack style grid shift
                 is_gamma_center=True,
                 is_time_reversal=True,
                 fit_in_BZ=True,
                 rotations=None,  # Point group operations in real space
                 is_mesh_symmetry=True):  # Except for time reversal symmetry
        """

        Note
        ----
        Uniform mesh grids are made according to Monkhorst-Pack scheme, i.e.,
        for odd (even) numbers, centre are (are not) sampled. The Gamma-centre
        sampling is supported by ``is_gamma_center=True``.

        Parameters
        ----------
        mesh_numbers: array_like
            Mesh numbers along a, b, c axes.
            dtype='intc'
            shape=(3, )
        reciprocal_lattice: array_like
            Basis vectors in reciprocal space. a*, b*, c* are given in column
            vectors.
            dtype='double'
            shape=(3, 3)
        q_mesh_shift: array_like, optional, default None (no shift)
            Mesh shifts along a*, b*, c* axes with respect to neighboring grid
            points from the original mesh (Monkhorst-Pack or Gamma center).
            0.5 gives half grid shift. Normally 0 or 0.5 is given.
            Otherwise q-points symmetry search is not performed.
            dtype='double'
            shape=(3, )
        is_gamma_center: bool, default False
            Uniform mesh grids are generated centring at Gamma point but not
            the Monkhorst-Pack scheme.
        is_time_reversal: bool, optional, default True
            Time reversal symmetry is considered in symmetry search. By this,
            inversion symmetry is always included.
        fit_in_BZ: bool, optional, default True
        rotations: array_like, default None (only unitary operation)
            Rotation matrices in direct space. For each rotation matrix R,
            a point in crystallographic coordinates, x, is sent as x' = Rx.
            dtype='intc'
            shape=(rotations, 3, 3)
        is_mesh_symmetry: bool, optional, default True
            Wheather symmetry search is done or not.

        """

        self._mesh = np.array(mesh_numbers, dtype='intc')
        self._rec_lat = reciprocal_lattice
        self._is_shift = self._shift2boolean(q_mesh_shift,
                                             is_gamma_center=is_gamma_center)
        self._is_time_reversal = is_time_reversal
        self._fit_in_BZ = fit_in_BZ
        self._rotations = rotations
        self._is_mesh_symmetry = is_mesh_symmetry

        self._ir_qpoints = None
        self._grid_address = None
        self._ir_grid_points = None
        self._ir_weights = None
        self._grid_mapping_table = None

        if self._is_shift is None:
            self._is_mesh_symmetry = False
            self._is_shift = self._shift2boolean(None)
            self._set_grid_points()
            self._ir_qpoints += q_mesh_shift / self._mesh
            self._fit_qpoints_in_BZ()
        else:  # zero or half shift
            self._set_grid_points()

    @property
    def mesh_numbers(self):
        return self._mesh

    @property
    def reciprocal_lattice(self):
        return self._rec_lat

    @property
    def grid_address(self):
        return self._grid_address

    def get_grid_address(self):
        warnings.warn("GridPoints.get_grid_address is deprecated."
                      "Use grid_address attribute.",
                      DeprecationWarning)
        return self.grid_address

    @property
    def ir_grid_points(self):
        return self._ir_grid_points

    def get_ir_grid_points(self):
        warnings.warn("GridPoints.get_ir_grid_points is deprecated."
                      "Use ir_grid_points attribute.",
                      DeprecationWarning)
        return self.ir_grid_points

    @property
    def qpoints(self):
        return self._ir_qpoints

    def get_ir_qpoints(self):
        warnings.warn("GridPoints.get_ir_qpoints is deprecated."
                      "Use points attribute.",
                      DeprecationWarning)
        return self.qpoints

    @property
    def weights(self):
        return self._ir_weights

    def get_ir_grid_weights(self):
        warnings.warn("GridPoints.get_ir_grid_weights is deprecated."
                      "Use weights attribute.",
                      DeprecationWarning)
        return self.weights

    @property
    def grid_mapping_table(self):
        return self._grid_mapping_table

    def get_grid_mapping_table(self):
        warnings.warn("GridPoints.get_grid_mapping_table is deprecated."
                      "Use grid_mapping_table attribute.",
                      DeprecationWarning)
        return self.grid_mapping_table

    def _set_grid_points(self):
        if self._is_mesh_symmetry and self._has_mesh_symmetry():
            self._set_ir_qpoints(self._rotations,
                                 is_time_reversal=self._is_time_reversal)
        else:
            self._set_ir_qpoints([np.eye(3, dtype='intc')],
                                 is_time_reversal=self._is_time_reversal)

    def _shift2boolean(self,
                       q_mesh_shift,
                       is_gamma_center=False,
                       tolerance=1e-5):
        """
        Tolerance is used to judge zero/half gird shift.
        This value is not necessary to be changed usually.
        """
        if q_mesh_shift is None:
            shift = np.zeros(3, dtype='double')
        else:
            shift = np.array(q_mesh_shift, dtype='double')

        diffby2 = np.abs(shift * 2 - np.rint(shift * 2))

        if (diffby2 < 0.01).all():  # zero or half shift
            diff = np.abs(shift - np.rint(shift))
            if is_gamma_center:
                is_shift = list(diff > 0.1)
            else:  # Monkhorst-pack
                is_shift = list(np.logical_xor((diff > 0.1),
                                               (self._mesh % 2 == 0)) * 1)
        else:
            is_shift = None

        return is_shift

    def _has_mesh_symmetry(self):
        if self._rotations is None:
            return False
        m = self._mesh
        mesh_equiv = [m[1] == m[2], m[2] == m[0], m[0] == m[1]]
        lattice_equiv = get_lattice_vector_equivalence(
            [r.T for r in self._rotations])
        return np.extract(lattice_equiv, mesh_equiv).all()

    def _fit_qpoints_in_BZ(self):
        qpoint_set_in_BZ = get_qpoints_in_Brillouin_zone(self._rec_lat,
                                                         self._ir_qpoints)
        qpoints_in_BZ = np.array([q_set[0] for q_set in qpoint_set_in_BZ],
                                 dtype='double', order='C')
        self._ir_qpoints = qpoints_in_BZ

    def _set_ir_qpoints(self,
                        rotations,
                        is_time_reversal=True):
        grid_mapping_table, grid_address = get_stabilized_reciprocal_mesh(
            self._mesh,
            rotations,
            is_shift=self._is_shift,
            is_time_reversal=is_time_reversal,
            is_dense=True)

        shift = np.array(self._is_shift, dtype='intc') * 0.5

        if self._fit_in_BZ:
            grid_address, _ = relocate_BZ_grid_address(
                grid_address,
                self._mesh,
                self._rec_lat,
                is_shift=self._is_shift,
                is_dense=True)
            self._grid_address = grid_address[:np.prod(self._mesh)]
        else:
            self._grid_address = grid_address

        (self._ir_grid_points,
         self._ir_weights) = extract_ir_grid_points(grid_mapping_table)

        self._ir_qpoints = np.array(
            (self._grid_address[self._ir_grid_points] + shift) / self._mesh,
            dtype='double', order='C')

        self._grid_mapping_table = grid_mapping_table


class GeneralizedRegularGridPoints(object):
    """Generalized regular grid points

    Method strategy in suggest mode
    -------------------------------
    1. Create conventional unit cell using spglib.
    2. Sample regular grid points for the conventional unit cell (mesh_numbers)
    3. Transformation matrix from primitive to conventinal unit cell (inv_pmat)
    4. Get supercell multiplicities (mesh_numbers) from the conventional unit
       cell considering the lattice shape.
    5. mmat = (inv_pmat * mesh_numbers).T, which is related to the
       transformation from primitive cell to supercell.
    6. D = P.mmat.Q, where D = diag([n1, n2, n3])
    7. Grid points for primitive cell are
       [np.dot(Q, g) for g in ndindex((n1, n2, n3))].

    Method strategy in non-suggest mode
    -----------------------------------
    1. Find symmetry operations
    2. Determine point group and transformation matrix (tmat) from input cell
    3. Get supercell multiplicities (mesh_numbers) from the transformed cell
       considering the lattice shape.
    4. mmat = (tmat * mesh_numbers).T
    5. D = P.mmat.Q, where D = diag([n1, n2, n3])
    6. Grid points for primitive cell are
       [np.dot(Q, g) for g in ndindex((n1, n2, n3))].

    Attributes
    ----------
    grid_address : ndarray
        Grid addresses in integers.
        shape=(num_grid_points, 3), dtype='intc', order='C'
    grid_matrix : ndarray
        Grid generating matrix.
        shape=(3,3), dtype='intc', order='C'
    matrix_to_primitive : ndarray or None
        None when ``suggest`` is False. Otherwise, transformation matrix from
        input cell to the suggested primitive cell.
        shape=(3,3), dtype='double', order='C'
    snf : SNF3x3
        SNF3x3 instance of grid generating matrix.

    """

    def __init__(self, cell, length, suggest=True, symprec=1e-5):
        """

        Parameters
        ----------
        cell : PhonopyAtoms
            Input cell.
        length : float
            Length having the unit of direct space length.

        """
        self._suggest = suggest
        self._grid_address = None
        self._snf = None
        self._matrix_to_primitive = None
        self._grid_matrix = None
        self._prepare(cell, length, symprec)
        self._generate_grid_points()

    @property
    def grid_address(self):
        return self._grid_address

    @property
    def grid_matrix(self):
        """Grid generating matrix"""
        return self._grid_matrix

    @property
    def matrix_to_primitive(self):
        """Transformation matrix to primitive cell"""
        return self._matrix_to_primitive

    @property
    def snf(self):
        """SNF3x3 instance of grid generating matrix"""
        return self._snf

    def _prepare(self, cell, length, symprec):
        """Define grid generating matrix and run the SNF"""

        self._sym_dataset = get_symmetry_dataset(
            cell.totuple(), symprec=symprec)
        if self._suggest:
            self._set_grid_matrix_by_std_primitive_cell(cell, length)
        else:
            self._set_grid_matrix_by_input_cell(cell, length)
        self._snf = SNF3x3(self._grid_matrix)
        self._snf.run()

    def _set_grid_matrix_by_std_primitive_cell(self, cell, length):
        """Grid generating matrix based on standeardized primitive cell"""

        tmat = self._sym_dataset['transformation_matrix']
        centring = self._sym_dataset['international'][0]
        pmat = get_primitive_matrix_by_centring(centring)
        conv_lat = np.dot(np.linalg.inv(tmat).T, cell.cell)
        num_cells = np.prod(length2mesh(length, conv_lat))
        mesh_numbers = estimate_supercell_matrix(
            self._sym_dataset,
            max_num_atoms=num_cells * len(self._sym_dataset['std_types']))
        inv_pmat = np.linalg.inv(pmat)
        inv_pmat_int = np.rint(inv_pmat).astype(int)
        assert (np.abs(inv_pmat - inv_pmat_int) < 1e-5).all()
        # transpose in reciprocal space
        self._grid_matrix = np.array(
            (inv_pmat_int * mesh_numbers).T, dtype='intc', order='C')
        self._matrix_to_primitive = np.array(
            np.dot(np.linalg.inv(tmat), pmat), dtype='double', order='C')

    def _set_grid_matrix_by_input_cell(self, cell, length):
        """Grid generating matrix based on input cell"""

        pointgroup = get_pointgroup(self._sym_dataset['rotations'])
        lattice = np.dot(cell.cell.T, pointgroup[2]).T
        num_cells = np.prod(length2mesh(length, lattice))
        mesh_numbers = estimate_supercell_matrix_from_pointgroup(
            pointgroup[1], lattice, num_cells)
        # transpose in reciprocal space
        self._grid_matrix = np.array(
            np.multiply(pointgroup[2], mesh_numbers).T,
            dtype='intc', order='C')

    def _generate_grid_points(self):
        d = np.diagonal(self._snf.D)
        x, y, z = np.meshgrid(range(d[0]), range(d[1]), range(d[2]),
                              indexing='ij')
        self._grid_address = np.array(np.c_[x.ravel(), y.ravel(), z.ravel()],
                                      dtype='intc', order='C')
