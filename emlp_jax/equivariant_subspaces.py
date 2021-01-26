import numpy as np
#import torch
import jax
from jax import jit
from functools import partial
import jax.numpy as jnp
from jax import device_put
import collections,itertools
from functools import lru_cache as cache
from emlp_jax.utils import disk_cache
from emlp_jax.linear_operator_jax import LinearOperator
import scipy as sp
import scipy.linalg
import functools
import random
import logging
import emlp_jax

class OrderedCounter(collections.Counter,collections.OrderedDict): pass

class Rep(object):
    def __eq__(self, other): raise NotImplementedError
    def size(self): raise NotImplementedError # dim(V) dimension of the representation
    def __add__(self, other): # Tensor sum representation R1 + R2
        if isinstance(other,int): return self+other*Scalar
        elif isinstance(other,Rep): return SumRep([self,other])
        else: return NotImplemented
    def __radd__(self,other):
        if isinstance(other,int): return other*Scalar+self
        elif isinstance(other,Rep): return SumRep([other,self])
        else: return NotImplemented
    def __mul__(self, other): 
        if isinstance(other,int): return SumRep(other*[self])
        else: return NotImplemented # Tensor product representation R1 x R2
    def __rmul__(self,other):
        if isinstance(other,int): return SumRep(other*[self])
        else: return NotImplemented # Tensor product representation R1 x R2
    @property
    def T(self): raise NotImplementedError # dual representation V*, rho*, drho*
    def __repr__(self): raise NotImplementedError
    def __str__(self): raise NotImplementedError 
    def __call__(self,G): raise NotImplementedError # set the symmetry group
    def rho(self,M): raise NotImplementedError # Group representation of matrix M (n,n)
    def drho(self,A): raise NotImplementedError # Lie Algebra representation of matrix A (n,n)

    def constraint_matrix(self):
        """ Given a sequence of exponential generators [A1,A2,...]
        and a tensor rank (p,q), the function concatenates the representations
        [drho(A1), drho(A2), ...] into a single large projection matrix.
        Input: [generators seq(tensor(d,d))], [rank tuple(p,q)], [d int] """
        constraints = []
        constraints.extend([self.drho(device_put(A)) for A in self.G.lie_algebra])
        constraints.extend([self.rho(device_put(h))-jnp.eye(self.size()) for h in self.G.discrete_generators])
        P = jnp.concatenate(constraints,axis=0) if constraints else jnp.zeros((1,self.size()))
        return P

    #@disk_cache('./_subspace_cache_jax.dat')
    @cache()
    def symmetric_basis(self):  
        """ Given an array of generators [M1,M2,...] and tensor rank (p,q)
            this function computes the orthogonal complement to the projection
            matrix formed by stacking the rows of drho(Mi) together.
            Output [Q (r,) + (p+q)*(d,)] """
        if self==Scalar: return jnp.ones((1,1))
        #if isinstance(group,Trivial): return np.eye(size(rank,group.d))
        Q = orthogonal_complement(self.constraint_matrix())
        return Q
    
    def symmetric_projector(self):
        Q = self.symmetric_basis()
        P = Q.T@Q
        return P

class TensorRep(Rep):
    def __init__(self,p,q=0,G=None):
        self.rank = (p+q,0) if G is not None and G.is_orthogonal else (p,q)
        self.G = G
    def rho(self,M): 
        if isinstance(M,dict): M = M[self.G]
        return tensor_rho(M,self.rank)
    def drho(self,A): 
        if isinstance(A,dict): A = A[self.G]
        return tensor_drho(A,self.rank)
    @property
    def T(self):
        if self.G.is_orthogonal: return self
        return TensorRep(*self.rank[::-1],G=self.G)
    def __mul__(self,other):
        if isinstance(other,int): return SumRep(other*[self])
        elif isinstance(other,TensorRep):
            if self.G==other.G: 
                return TensorRep(self.rank[0]+other.rank[0],self.rank[1]+other.rank[1],
                                G=self.G if self.G is None else other.G)
            else:
                return emlp_jax.mixed_tensors.ProductGroupTensorRep({self.G:self,other.G:other}) #Order matters?
        else: return NotImplemented
    def __mod__(self,other): # Wreath product
        raise NotImplementedError
    def __rmul__(self,other): 
        if isinstance(other,int): return SumRep(other*[self])
        else: return NotImplemented
    def size(self):
        return self.G.d**sum(self.rank)

    def __repr__(self): return str(self)#f"T{self.rank+(self.G,)}"
    def __str__(self):
        p,q = self.rank 
        return f"T{(p,q)}" if self.G is None or not self.G.is_orthogonal else f"T({p+q})"
    def __hash__(self):
        if self.rank==(0,0): return hash(self.rank) # Scalars are scalars regardless of the group
        return hash((self.rank,self.G))
    def __eq__(self,other):
        return isinstance(other,TensorRep) and self.rank==other.rank and (self.rank==(0,0) or self.G==other.G)
    def __call__(self,G):
        self.G=G
        if G.is_orthogonal: self.rank = (sum(self.rank),0) 
        return self
    def show_subspace(self):
        dims,projection = self.symmetric_basis()
        vals = projection(jnp.arange(dims)+1)
        return jnp.where(jnp.abs(vals)>1e-7,vals,jnp.zeros_like(vals)).reshape(*[self.G.d for _ in range(sum(self.rank))])

class T(TensorRep): pass # A short alias for TensorRep

class SumRep(Rep):
    def __init__(self,reps,shapes=None):
        """ Constructs a tensor type based on a list of tensor ranks
            and possibly the symmetry generators gen."""
        self.reps = reps
        self.shapes = (self.reps,) if shapes is None else shapes
        # shapes how the reps are arranged when in matrix or tensor form
        # ie R1+R2 self.shapes = ([R1,R2],)
        # R1 x (R2+R3) sel.shapes = ([R1],[R2,R3])
        #TODO: support more general expressions e.g. R1 x (R2+R3) + R4 x R5 ?

    def __eq__(self, other):
        return len(self)==len(other) and all(r==rr for r,rr in zip(self.reps,other.reps))# and self.G==other.G
    def __len__(self):
        return len(self.reps)
    @property
    def shape(self): # Returns the total shape of the object the rep represents, e.g. (cin,cout) for a matrix
        return tuple(sum(rep.size() for rep in reps) for reps in self.shapes)
    def size(self):
        return sum(rep.size() for rep in self.reps)
    # def __call__(self,G):
    #     self.G=G
    #     self.d = self.G.d
    #     return self
    def __add__(self, other):
        if isinstance(other,int):
            return self+other*Scalar
        elif isinstance(other,SumRep):
            return SumRep(self.reps+other.reps)
        elif isinstance(other,Rep):
            return SumRep(self.reps+[other])
        else:
            return NotImplemented
    def __radd__(self, other):
        if isinstance(other,int):
            return other*Scalar+self
        elif isinstance(other,Rep):
            return SumRep([other]+self.reps)
        else: assert False, f"Unsupported operand Rep.__radd__{type(other)}"
    def __mul__(self, other):
        logging.debug(f"on __mul__ with {type(other)}")
        if isinstance(other,int): return SumRep(other*self.reps)
        elif isinstance(other,SumRep): #TODO: account for the shape?
            return SumRep([rep1*rep2 for rep1,rep2 in itertools.product(self.reps,other.reps)],self.shapes+other.shapes)
        elif isinstance(other,Rep):
            return SumRep([rep*other for rep in self.reps])
        else: assert False, f"Unsupported operand Rep.__mul__{type(other)}"

    def __rmul__(self, other):
        if isinstance(other, int): return SumRep(other*self.reps)
        elif isinstance(other,Rep): return SumRep([other*rep for rep in self.reps])
        else: assert False, f"Unsupported operand Rep.__rmul__{type(other)}"
    def __call__(self,G):
        self.reps = [rep(G) for rep in self.reps]
        return self
    # def __iter__(self):
    #     return iter(self.ranks)
    @property
    def T(self):
        """ only swaps to adjoint representation, does not reorder elems"""
        return SumRep([rep.T for rep in self.reps])

    def multiplicities(self):
        return OrderedCounter(self.reps)

    def __repr__(self):
        return "+".join(f"{count if count > 1 else ''}{repr(rep)}" for rep, count in self.multiplicities().items())
    def __str__(self):
        tensors = "+".join(f"{count if count > 1 else ''}{rep}" for rep, count in self.multiplicities().items())
        return tensors#+f" @ d={self.d}" if self.d is not None else tensors

    def __hash__(self):
        return hash(tuple(self.reps))

    def symmetric_basis(self):
        """ Given a representation which is a sequence of tensors
        with ranks (p_i,q_i), computes the orthogonal complement
        to the projection matrix drho(Mi). Function returns both the
        dimension of the active subspace (r) and also a function that
        maps an array of size (*,r) to a vector v with a representaiton
        given by the rnaks that satisfies drho(Mi)v=0 for each i.
        Inputs: [generators seq(tensor(d,d))] [ranks seq(tuple(p,q))]
        Outputs: [r int] [projection (tensor(r)->tensor(rep_dim))]"""
        rep_multiplicites = self.multiplicities()
        Qs = {rep: rep.symmetric_basis() for rep in rep_multiplicites}
        Qs = {rep: jax.device_put(Q.astype(np.float32)) for rep,Q in Qs.items()}
        active_dims = sum([rep_multiplicites[rep]*Qs[rep].shape[0] for rep in Qs.keys()])
        # Get the permutation of the vector when grouped by tensor rank
        inverse_perm = jnp.argsort(self.argsort())
        # Apply the projections for each rank, concatenate, and permute back to orig rank order
        block_perm = rep_permutation(self)
        def lazy_Q(array):
            i=0
            Ws = []
            for rep, multiplicity in rep_multiplicites.items():
                Qr = Qs[rep]
                i_end = i+multiplicity*Qr.shape[0]
                elems = array[...,i:i_end].reshape(*array.shape[:-1],multiplicity,Qr.shape[0])@Qr
                Ws.append(elems.reshape(*array.shape[:-1],multiplicity*rep.size()))
                i = i_end
            Ws = jnp.concatenate(Ws,axis=-1) #concatenate over rep axis
            inp_ordered_Ws = Ws[...,inverse_perm] # reorder to original rep ordering 
            if len(self.shape)>1: 
                #Also only allows r -> shape (ie a (*shape,r)) matrix
                inp_ordered_Ws = inp_ordered_Ws[block_perm] #TODO fix shape transpose vec op
            return  inp_ordered_Ws
        
        return LinearOperator(shape=(self.size(),active_dims),matvec = lazy_Q)
        

    def symmetric_projector(self):
        rep_multiplicites = self.multiplicities()
        Ps = {rep:rep.symmetric_projector() for rep in rep_multiplicites}
        logging.debug("finished projector")
        #Qs = {rep:jax.device_put(Q.astype(np.float32)) for rep,Q in Qs.items()}
        # Get the permutation of the vector when grouped by tensor rep
        perm = self.argsort()
        invperm = jnp.argsort(perm)
        block_perm = rep_permutation(self)
        inv_block_perm = jnp.argsort(block_perm)
        logging.debug("finished argsorting")
        # Apply the projections for each rep, concatenate, and permute back to orig rep order
        def lazy_QQT(W):
            print(f"W shape {W.shape}")
            ordered_W = W[inv_block_perm][perm] if len(self.shape)>1 else W[perm]
            PWs = []
            i=0
            for rep, multiplicity in rep_multiplicites.items():
                P = Ps[rep]
                i_end = i+multiplicity*rep.size()
                PWs.append((P@ordered_W[i:i_end].reshape(multiplicity,rep.size()).T).T.reshape(-1))
                i = i_end
            PWs = jnp.concatenate(PWs,axis=-1) #concatenate over rep axis
            print(f"QQT shape {self.shape}")
            inp_ordered_PWs = PWs[invperm][block_perm] if len(self.shape)>1 else PWs[invperm]
            return  inp_ordered_PWs # reorder to original rep ordering
        return LinearOperator(shape=(self.size(),self.size()),matvec=lazy_QQT)

    def show_subspace(self):
        Q = self.symmetric_basis()
        vals = Q@(jnp.arange(Q.shape[-1])+1)
        return jnp.where(jnp.abs(vals)>1e-7,vals,jnp.zeros_like(vals)).reshape(*self.shape)

    def rho(self,M): #Incorrect rho for tensor products of sums? Needs to be permuted
        #TODO: add switching to use lazy matrices depending on size
        rho_blocks = jax.scipy.linalg.block_diag(*[rep.rho(M) for rep in self.reps])
        block_perm = rep_permutation(self)
        inv_block_perm = jnp.argsort(block_perm)
        #print(rho_blocks.shape,block_perm.shape,inv_block_perm.shape)
        #print(block_perm)
        out = rho_blocks[inv_block_perm,:][:,block_perm]#[inv_block_perm,block_perm]
        #print(out.shape)
        return out

    def drho(self,A): #Incorrect rho for tensor products of sums? Needs to be permuted
        #TODO: add switching to use lazy matrices depending on size
        return jax.scipy.linalg.block_diag(*[rep.drho(A) for rep in self.reps])

    def argsort(self):
        """ get the permutation given by converting
            from the order in ranks to the order when the ranks are grouped by
            first occurrence of a given type (p,q). (Bucket sort)"""
        ranks_indices = collections.defaultdict(list)
        i=0
        for rep in self.reps:
            ranks_indices[rep].append(np.arange(rep.size())+i)
            i+= rep.size()
        permutation = np.concatenate([np.concatenate(indices) for indices in ranks_indices.values()])
        return permutation

Scalar = T(0,0)
Vector = T(1,0)
Matrix = T(1,1)
Quad = T(0,2)

@partial(jit,static_argnums=(1,))
def tensor_rho(G,rank):
    """ Representation matrix rho(g) for the tensor T(p,q)"""
    if rank ==(0,0): return jnp.ones((1,1))
    p,q = rank
    Gp = functools.reduce(jnp.kron,p*[G],1)
    GpGinvTq = functools.reduce(jnp.kron,q*[jnp.linalg.inv(G).T],Gp) # shouldn't this be backwards?
    return GpGinvTq

@partial(jit,static_argnums=(1,))
def tensor_drho(M,rank):
    """ Returns the Lie Algebra representation drho(M) of a matrix M
        acting on a rank (p,q) tensor.
        Inputs: [M (d,d)] [rank tuple(p,q)]
        Outputs: [drho(M) (d**(p+q),d**(p+q))]"""
    if rank ==(0,0): return jnp.zeros((1,1))
    p,q = rank
    d=M.shape[0]
    rep_M = 0
    Ikron_powers = [1]
    for _ in range(p+q-1):
        Ikron_powers.append(jnp.kron(Ikron_powers[-1],jnp.eye(d)))
    for r in range(1,p+1):
        rep_M += jnp.kron(jnp.kron(Ikron_powers[r-1],M),Ikron_powers[p-r+q])
    for s in range(1,q+1):
       rep_M -= jnp.kron(jnp.kron(Ikron_powers[p+s-1],M.T),Ikron_powers[q-s])
    return rep_M

class tensor_rho_lazy(LinearOperator):
    def __init__(self,M,rank):
        self.d = M.shape[0]
        self.M = M
        self.rank = rank
        self.c = size(rank,self.d)
        self.dtype=np.float64
    @property
    def shape(self):
        return (self.c,self.c)
    def _matmat(self,V): #(c,k) #Still needs to be tested??
        c,k = V.shape
        p,q = self.rank
        MinvT = np.linalg.inv(self.M.T)
        eV = V.reshape((p+q)*[self.d]+[k])
        for i in range(p):
            eV = np.moveaxis(np.dot(self.M,np.moveaxis(eV,i,0)),0,i)
        for i in range(p,p+q):
            eV = np.moveaxis(np.dot(MinvT,np.moveaxis(eV,i,0)),0,i)
        return eV.reshape(*V.shape)
    def _adjoint(self):
        return rho_lazy(self.M.T,self.rank)

class tensor_drho_lazy(LinearOperator):
    def __init__(self,M,rank):
        self.d = M.shape[0]
        self.M = M
        self.rank = rank
        self.c = size(rank,self.d)
        self.dtype=np.float32
    @property
    def shape(self):
        return (self.c,self.c)
    def _matmat(self,V): #(c,k)
        c,k = V.shape
        p,q = self.rank
        eV = V.reshape((p+q)*[self.d]+[k])
        out = np.zeros_like(eV)
        for i in range(p):
            out += np.moveaxis(np.dot(self.M,np.moveaxis(eV,i,0)),0,i)
        for i in range(p,p+q):
            out -= np.moveaxis(np.dot(self.M.T,np.moveaxis(eV,i,0)),0,i)
        return out.reshape(*V.shape)
    def _adjoint(self):
        return drho_lazy(self.M.T,self.rank)

class tensor_projection_lazy(LinearOperator):
    def __init__(self,group,rank):
        self.d = group.d
        self.hi = group.discrete_generators
        self.Ai = group.lie_algebra
        self.G=group
        self.n_constraints= len(self.hi)+len(self.Ai)
        if not self.n_constraints: raise NotImplementedError
        self.rank = rank
        self.c = size(rank,self.d)
        self.dtype=np.float32
    @property
    def shape(self):
        return (self.c*self.n_constraints,self.c)
    def _matmat(self,V): #(c,k)
        constraints = []
        constraints.extend([drho_lazy(A,self.rank)@V for A in self.Ai])
        constraints.extend([rho_lazy(h,self.rank)@V-V for h in self.hi])
        CV = np.concatenate(constraints,axis=0)
        return CV
    def _rmatmat(self,V):
        n_constraints = len(self.hi)+len(self.Ai)
        Vi = np.split(V,self.n_constraints)
        out = 0
        out += sum([drho_lazy(A,self.rank).T@Vi[i] for i,A in enumerate(self.Ai)])
        out += sum([rho_lazy(h,self.rank).T@Vi[i+len(self.Ai)] for i,h in enumerate(self.hi)])
        return out

def orthogonal_complement(proj):
    """ Computes the orthogonal complement to a given matrix proj"""
    U,S,VT = jnp.linalg.svd(proj,full_matrices=True) # Changed from full_matrices=True
    rank = (S>1e-5).sum()
    return VT[rank:]

#@partial(jit,static_argnums=(0,1))
def bilinear_weights(W_rep,x_rep):
    W_multiplicities = W_rep.multiplicities()
    x_multiplicities = x_rep.multiplicities()
    x_multiplicities = {rep:n for rep,n in x_multiplicities.items() if rep!=Scalar}
    nelems = lambda nx,rep: min(nx,rep.size())
    active_dims = sum([W_multiplicities[rep]*nelems(n,rep) for rep,n in x_multiplicities.items()])
    # Get the permutation of the vector when grouped by tensor rank
    inverse_perm = jnp.argsort(W_rep.argsort())
    rank_indices_dict = tensor_indices_dict(x_rep)
    reduced_indices_dict = {rep:jnp.concatenate(random.sample(ids,nelems(len(ids),rep)))\
                                for rep,ids in rank_indices_dict.items()}
    block_perm = rep_permutation(W_rep)
    # Apply the projections for each rank, concatenate, and permute back to orig rank order
    def lazy_projection(params,x): # (*,r), (bs,c) #TODO: find out why backwards of this function is so slow
        bs = x.shape[0]
        i=0
        Ws = []
        for rep, W_mult in W_multiplicities.items():
            if rep not in x_multiplicities:
                Ws.append(jnp.zeros((bs,W_mult*rep.size())))
                continue
            x_mult = x_multiplicities[rep]
            n = nelems(x_mult,rep)
            i_end = i+W_mult*n
            bids =  reduced_indices_dict[rep]
            bilinear_params = params[i:i_end].reshape(W_mult,n)
            i = i_end  # (bs,W_mult,d^r) = (W_mult,n)@(n,d^r,bs)
            bilinear_elems = bilinear_params@x[:,bids].T.reshape(n,rep.size()*bs)
            bilinear_elems = bilinear_elems.reshape(W_mult*rep.size(),bs).T
            Ws.append(bilinear_elems)
        Ws = jnp.concatenate(Ws,axis=-1) #concatenate over rep axis
        return Ws[:,inverse_perm][:,block_perm].reshape(-1,*W_rep.shape) # reorder to original rank ordering
    return active_dims,lazy_projection

#@cache()
def tensor_indices_dict(sumrep):
    index_dict = collections.defaultdict(list)
    i=0
    for rep in sumrep.reps:
        i_end = i+rep.size()
        index_dict[rep].append(jnp.arange(i,i_end))
        i = i_end
    return index_dict#{rank:np.concatenate(ids) for rank,ids in index_dict.items()}

@cache()
def rep_permutation(sumrep):
    """Permutation from flattened ordering to block ordering """
    arange = np.arange(sumrep.size())
    size_cumsums = [np.cumsum([0] + [rep.size() for rep in reps]) for reps in sumrep.shapes]
    permutation = np.zeros([cumsum[-1] for cumsum in size_cumsums]).astype(np.int)
    indices_iter = itertools.product(*[range(len(reps)) for reps in sumrep.shapes])
    i = 0
    for indices in indices_iter:
        slices = tuple([slice(cumsum[idx], cumsum[idx + 1]) for idx, cumsum in zip(indices, size_cumsums)])
        slice_lengths = [sl.stop - sl.start for sl in slices]
        chunk_size = np.prod(slice_lengths)
        permutation[slices] += arange[i:i + chunk_size].reshape(*slice_lengths)
        i += chunk_size
    return permutation.reshape(-1)

