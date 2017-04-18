from functools import wraps

# Inspired by create_cookies() in the requests module
def _restrictive_update(old_dict, new_dict):
    '''Update a dictionary, but throw an error if trying to add new keys'''

    new_keys = set(new_dict) - set(old_dict)
    if new_keys:
        err = 'Got invalid keyword arguments: {}.'
        raise AttributeError( err.format(list(new_keys)) )
    else:
        old_dict.update(new_dict)

def defaultkwargs(default_name='_defaults'):
    ''' Compare a function's keyword arguments to a dictionary of defaults
        
        Arguments:
        default_name -- The name of a dictionary which contains default
        values. If @defaultkwargs is wrapping a method of class A, then
        A.default_name should be a valid (prefereably class-level)
        attribute

        Usage:
        Assume @defaultkwargs() is wrapping some method of class A
        called A.func. When A.func(**kwargs) is called, @defaultkwargs
        will copy A._defaults to a dictionary new_kwargs, update new_kwargs
        with kwargs, and call A.func(**new_kwargs) instead. The update is
        performed with _restrictive_update(), so that any keys in kwargs
        not also in A._defaults will raise an error.

    '''
    
    def decorate(f):

        @wraps(f)
        def wrapper(self, *args, **kwargs):
            new_kwargs = dict(self.__getattribute__(default_name)) # Make a deep copy
            _restrictive_update(new_kwargs, kwargs)
            return f(self, *args, **new_kwargs)

        return wrapper
    return decorate
