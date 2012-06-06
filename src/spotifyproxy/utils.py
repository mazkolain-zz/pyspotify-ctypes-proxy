'''
Created on 06/06/2012

@author: mikel
'''
import inspect
from spotify.utils.weakmethod import WeakMethod


class DynamicCallback:
    __callback = None
    
    
    def set_callback(self, callback):
        if inspect.isfunction(callback):
            self.__callback = callback
        
        elif inspect.ismethod(callback):
            self.__callback = WeakMethod(callback)
        
        else:
            raise TypeError('Only functions and method are accepted as arguments.')
    
    
    def clear_callback(self):
        self.__callback = None
    
    
    def __call__(self, *args, **kwargs):
        if self.__callback is not None:
            self.__callback(*args, **kwargs)
