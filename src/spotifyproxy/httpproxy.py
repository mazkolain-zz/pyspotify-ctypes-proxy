'''
Created on 06/05/2011

@author: mikel
'''
import threading

import cherrypy


class Image:
    __session = None
    
    
    def __init__(self, session):
        self.__session = session
    
    
    @cherrypy.expose
    def default(self, image_id):
        return "image"



class Track:
    __session = None
    __is_playing = None
    
    
    def __init__(self, session):
        self.__is_playing = False
    
    
    @cherrypy.expose
    def default(self, track_id):
        return "track requested: %s" % track_id
    
    default._cp_config = {'response.stream': True}



class Root:
    __session = None
    
    image = None
    track = None
    
    
    def __init__(self, session):
        self.__session = session
        self.image = Image(session)
        self.track = Track(session)



class ProxyRunner(threading.Thread):
    def __init__(self, session):
        threading.Thread.__init__(self)
        cherrypy.tree.mount(Root(session), "/")
    
    def run(self):
        cherrypy.engine.start()
        cherrypy.engine.block()
    
    def stop(self):
        cherrypy.engine.exit()
