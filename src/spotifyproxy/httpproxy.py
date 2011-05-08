'''
Created on 06/05/2011

@author: mikel
'''
import threading

#Why the hell "import spotify" does not work?
from spotify import image as _image, BulkConditionChecker

import cherrypy



class ImageCallbacks(_image.ImageCallbacks):
    __checker = None
    
    
    def __init__(self, checker):
        self.__checker = checker
    
    
    def image_loaded(self, image):
        self.__checker.check_conditions()



class Image:
    __session = None
    
    
    def __init__(self, session):
        self.__session = session
    
    
    @cherrypy.expose
    def default(self, image_id):
        img = _image.create(self.__session, image_id)
        checker = BulkConditionChecker()
        img_cb = ImageCallbacks(checker)
        img.add_load_callback(img_cb)
        checker.complete_wait(10)
        
        #Fail if image was not loaded or wrong format
        if not img.is_loaded() or img.format() != _image.ImageFormat.JPEG:
            raise cherrypy.HTTPError(500)
        
        else:
            cherrypy.response.headers["Content-Type"] = "image/jpeg"
            return img.data()



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
