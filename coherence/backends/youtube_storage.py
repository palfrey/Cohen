# Licensed under the MIT license
# http://opensource.org/licenses/mit-license.php

# Copyright 2009, Jean-Michel Sizun
# Copyright 2009 Frank Scholz <coherence@beebits.net>

import os.path

from twisted.internet import reactor, threads

from twisted.web import server, static
from twisted.web.error import PageRedirect

from coherence.upnp.core import utils
from coherence.upnp.core import DIDLLite
from coherence.backend import BackendStore,BackendItem

from urlparse import urlsplit

from gdata.youtube.service import YouTubeService
from coherence.extern.youtubedl import FileDownloader,YoutubeIE,MetacafeIE,YoutubePlaylistIE

ROOT_CONTAINER_ID = 0
MY_PLAYLISTS_CONTAINER_ID = 100
MY_SUBSCRIPTIONS_CONTAINER_ID = 101
MPEG4_MIMETYPE = 'video/mp4'

class VideoProxy(utils.ReverseProxyResource):

    def __init__(self, uri, id, proxy_mode, 
                 cache_directory, cache_maxsize=100000000, buffer_size=2000000, 
                 fct=None, **kwargs):
        self.uri = uri
        self.id = id
        self.proxy_mode = proxy_mode
        self.video_url = None # the url we get from the youtube page
        self.stream_url = None # the real video stream, cached somewhere
        self.mimetype = None
        self.filesize = 0
        self.file_in_cache = False
        self.cache_directory = cache_directory
        self.cache_maxsize = int(cache_maxsize)
        self.buffer_size = int(buffer_size)
        self.downloader = None
        self.url_extractor_fct = fct
        self.url_extractor_params = kwargs
        host,port,path,params =  self.splitUri(uri)
        if params == '':
            rest = path
        else:
            rest = '%s?%s' % (path, params)  
        utils.ReverseProxyResource.__init__(self, host, port, rest)

    def splitUri (self, uri):
        _,host_port,path,params,_ = urlsplit(uri)
        if host_port.find(':') != -1:
            host,port = tuple(host_port.split(':'))
            port = int(port)
        else:
            host = host_port
            port = 80
        if path == '':
            path = '/'
        return host, port, path, params

    def resetUri (self, uri):
        host,port,path,params =  self.splitUri(uri)
        self.uri = uri
        if params != '':
            self.rest = '%s?%s' % (path, params)
        else:
            self.rest = path
        self.resetTarget(host, port, path)

    def requestFinished(self, result):
        """ self.connection is set in utils.ReverseProxyResource.render """
        print "ProxyStream requestFinished"
        if hasattr(self,'connection'):
            self.connection.transport.loseConnection()
    
            
    def render(self, request):

        print "VideoProxy render", request, self.stream_url, self.video_url

        if self.stream_url is None:

            web_url = "http://%s%s" % (self.host,self.path)
            print "web_url", web_url

            def got_real_urls(real_urls):
                got_real_url(real_urls[0])

            def got_real_url(real_url):
                print "Real URL is %s" % real_url
                self.stream_url = real_url
                if self.stream_url is None:
                    print 'Error to retrieve URL - inconsistent web page'
                    return requestFinished(result) #FIXME
                self.stream_url = self.stream_url.encode('ascii', 'strict')
                self.resetUri(self.stream_url)
                print "Video URL: %s" % self.stream_url
                self.video_url = self.stream_url[:]
                self.followRedirects(request, self.proxyURL, request)

            if self.url_extractor_fct is not None:
                d = self.url_extractor_fct(web_url, **self.url_extractor_params)
                d.addCallback(got_real_urls)
            else:
                got_real_url(web_url)
            return server.NOT_DONE_YET

        reactor.callLater(0.1,self.proxyURL,request)
        return server.NOT_DONE_YET

    def followRedirects(self, request, callback, *args):
        print "HTTP redirect", request, self.stream_url
        d = utils.getPage(self.stream_url, method="HEAD", followRedirect=0)

        def gotHeader(result,request, callback, *args):
            data,header = result
            self.filesize = int(header['content-length'][0])
            self.mimetype = header['content-type'][0]
            callback(*args)
            
        def gotError(error,request):
            # error should be a "Failure" instance at this point
            error_value = error.value
            if (isinstance(error_value,PageRedirect)):
                self.stream_url = error_value.location
                self.resetUri(self.stream_url)
                self.followRedirects(request, callback, *args)
            else:
                print "Error while retrieving page header for URI ", self.stream_url, error
                return requestFinished(result) #FIXME
            
        d.addCallback(gotHeader,request, callback, *args)
        d.addErrback(gotError,request)

    
    def proxyURL(self, request):
        print "proxy_mode: %s" % self.proxy_mode
        
        if self.proxy_mode == 'redirect':
            # send stream url to client for redirection
            request.redirect(self.stream_url)
            request.finish()
            
        elif self.proxy_mode == 'cache':
            # downloaded stream to cache,
            # and then send it to the client
            filepath = '%s/%s' % (self.cache_directory, self.id)
            if (os.path.exists(filepath) 
                and os.path.getsize(filepath) == self.filesize):
                self.renderFile(None, request, filepath)
            else:
                self.downloadFile(request, filepath, self.renderFile)
                
        elif self.proxy_mode == 'buffered':
            # download stream to cache,
            # and send it to the client in // after X bytes
            filepath = '%s/%s' % (self.cache_directory, self.id)           
            file_is_already_available = False
            if (os.path.exists(filepath) 
                and os.path.getsize(filepath) == self.filesize):
                self.renderFile(None, request, filepath)
            else:
                self.downloadFile(request, filepath, None)
                self.renderBufferFile (request, filepath, self.buffer_size)
                
        else:
            print "Unsupported Proxy Mode: %s" % self.proxy_mode
            return requestFinished(result)        
     
    def renderFile(self, result, request, filepath):
        print 'Cache file available'
        downloadedFile = utils.StaticFile(filepath, self.mimetype)
        res = downloadedFile.render(request)
        

    def renderBufferFile (self, request, filepath, buffer_size):
        # Try to render file(if we have enough data)
        print "renderBufferFile %s" % filepath
        rendering = False
        if os.path.exists(filepath) is True:
            filesize = os.path.getsize(filepath)
            if ((filesize >= buffer_size) or (filesize == self.filesize)):
                rendering = True
                print "Render file", filepath, self.filesize, filesize, buffer_size 
                bufferFile = utils.BufferFile(filepath, self.filesize, MPEG4_MIMETYPE)
                try:
                    res = bufferFile.render(request)
                except Exception,error:
                    print error

        # if unsucessfull, we will retry later
        if (rendering is False):
            print 'Will retry later to render buffer file'
            reactor.callLater(5.0, self.renderBufferFile, request, filepath, self.buffer_size)
        
        return rendering
 
    def downloadFinished(self, result):
        print 'Download finished!'
        self.downloader = None

    def gotDownloadError(self, error, request):
        print "Unable to download stream to file: %s" % self.stream_url
        print request
        print error  
        
    def downloadFile(self, request, filepath, callback, *args):       
        if (self.downloader is None):
            print "Proxy: download data to cache file %s" % filepath
            self.checkCacheSize()
            self.downloader = utils.downloadPage(self.stream_url, filepath, supportPartial=1)
            self.downloader.addCallback(self.downloadFinished)
        if(callback is not None):
            self.downloader.addCallback(callback, request, filepath, *args)
        self.downloader.addErrback(self.gotDownloadError, request)
        return self.downloader


    def checkCacheSize(self):
        cache_listdir = os.listdir(self.cache_directory)
        
        cache_size = 0
        for filename in cache_listdir:
            path = "%s%s%s" % (self.cache_directory, os.sep, filename)
            statinfo = os.stat(path)
            cache_size += statinfo.st_size
        print "Cache size: %d (max is %s)" % (cache_size, self.cache_maxsize)
        
        if (cache_size > self.cache_maxsize):
            cache_targetsize = self.cache_maxsize * 2/3
            print "Cache above max size: Reducing to %d" % cache_targetsize
             
            def compare_atime(filename1, filename2):
                path1 = "%s%s%s" % (self.cache_directory, os.sep, filename1)
                path2 = "%s%s%s" % (self.cache_directory, os.sep, filename2)
                cmp = int(os.stat(path2).st_atime - os.stat(path1).st_atime)
                return cmp
            cache_listdir = sorted(cache_listdir,compare_atime)
            

            while (cache_size > cache_targetsize):
                filename = cache_listdir.pop(0)
                path = "%s%s%s" % (self.cache_directory, os.sep, filename)
                cache_size -= os.stat(path).st_size
                os.remove(path)
                print "removed %s" % filename
                
            print "new cache size is %d" % cache_size
            


class YoutubeVideoItem(BackendItem):

    def __init__(self, store, parent, id, external_id, title, url, mimetype, entry):
        self.parent = parent
        self.id = id
        self.external_id = external_id
        self.name = title
        self.duration = None
        self.size = None
        self.mimetype = mimetype
        self.description = None
        self.date = None
        self.item = None
        self.store = store
        self.url = self.store.urlbase + str(self.id)
        
        def extractDataURL(url, quality):
            if (quality == 'hd'):
                format = '22'
            else:
                format = '18'
                    
            kwargs = {
                'usenetrc': False,
                'quiet': True,
                'forceurl': True,
                'forcetitle': False,
                'simulate': True,
                'format': format,
                'outtmpl': u'%(id)s.%(ext)s',
                'ignoreerrors': True,
                'ratelimit': None,
                }
            if len(self.store.login) > 0:
                kwargs['username'] = self.store.login
                kwargs['password'] = self.store.password
            fd = FileDownloader(kwargs)
    
            youtube_ie = YoutubeIE()
            fd.add_info_extractor(YoutubePlaylistIE(youtube_ie))
            fd.add_info_extractor(MetacafeIE(youtube_ie))
            fd.add_info_extractor(youtube_ie)
            
            deferred = fd.get_real_urls([url])
            return deferred
        
        self.location = VideoProxy(url, self.external_id, 
                                   store.proxy_mode, 
                                   store.cache_directory, store.cache_maxsize, store.buffer_size,
                                   extractDataURL, quality=self.store.quality)


    def get_item(self):
        if self.item == None:
            self.item = DIDLLite.VideoItem(self.id, self.parent.id, self.name)
            self.item.description = self.description
            self.item.date = self.date

            if hasattr(self.parent, 'cover'):
                self.item.albumArtURI = self.parent.cover

            res = DIDLLite.Resource(self.url, 'http-get:*:%s:*' % self.mimetype)
            res.duration = self.duration
            res.size = self.size
            self.item.res.append(res)
        return self.item

    def get_path(self):
        return self.url


class Container(BackendItem):

    def __init__(self, id, store, parent_id, title):
        self.url = store.urlbase+str(id)
        self.parent_id = parent_id
        self.id = id
        self.name = title
        self.mimetype = 'directory'
        self.update_id = 0
        self.children = []
        self.store = store

        self.item = DIDLLite.Container(self.id, self.parent_id, self.name)
        self.item.childCount = 0

        self.sorted = False

    def add_child(self, child):
        id = child.id
        if isinstance(child.id, basestring):
            _,id = child.id.split('.')
        if self.children is None:
            self.children = []
        self.children.append(child)
        self.item.childCount += 1
        self.sorted = False

    def get_children(self, start=0, end=0):
        if self.sorted == False:
            def childs_sort(x,y):
                r = cmp(x.name,y.name)
                return r

            self.children.sort(cmp=childs_sort)
            self.sorted = True
        if end != 0:
            return self.children[start:end]
        return self.children[start:]

    def get_child_count(self):
        if self.children is None:
            return 0
        return len(self.children)

    def get_path(self):
        return self.url

    def get_item(self):
        return self.item

    def get_name(self):
        return self.name

    def get_id(self):
        return self.id


class LazyContainer(Container):

    def __init__(self, id, store, parent_id, title):
        Container.__init__(self, id, store, parent_id, title)
        self.children = None

    def retrieve_children(self):
        return None
    
    def get_children(self,start=0,request_count=0):

        def process_items(result = None):
            if self.children == None:
                return  []
            if request_count == 0:
                return self.children[start:]
            else:
                return self.children[start:request_count]

        if (self.children == None):
            d = self.retrieve_children()
            if d is not None:
                d.addCallback(process_items)
            return d
        else:
            return process_items()
   

class YoutubeFeed(LazyContainer):
    def __init__(self, id, store, parent_id, title, feed_uri):
        LazyContainer.__init__(self, id, store, parent_id, title)
        self.feed_uri = feed_uri
    def retrieve_children(self):
        return self.store.retrieveFeedItems (self, self.feed_uri)
    
class YoutubePlaylistContainer(LazyContainer):
    def __init__(self, id, store, parent_id, title):
        LazyContainer.__init__(self, id, store, parent_id, title)
    def retrieve_children(self):
        return self.store.retrievePlaylistFeeds (self)

class YoutubeSubscriptionContainer(LazyContainer):
    def __init__(self, id, store, parent_id, title):
        LazyContainer.__init__(self, id, store, parent_id, title)
    def retrieve_children(self):
        return self.store.retrieveSubscriptionFeeds (self)

class YoutubePlaylistFeed(LazyContainer):
    def __init__(self, id, store, parent_id, title, playlist_feed_id):
        LazyContainer.__init__(self, id, store, parent_id, title)
        self.playlist_feed_id = playlist_feed_id       
    def retrieve_children(self):
        return self.store.retrievePlaylistFeedItems (self, self.playlist_feed_id)

class YoutubeSubscriptionFeed(LazyContainer):
    def __init__(self, id, store, parent_id, title, subscription_feed_id):
        LazyContainer.__init__(self, id, store, parent_id, title)
        self.subscription_feed_id = subscription_feed_id       
    def retrieve_children(self):
        return self.store.retrieveSubscriptionFeedItems (self, self.subscription_feed_id)

 
class YouTubeStore(BackendStore):

    logCategory = 'youtube_store'

    implements = ['MediaServer']

    wmc_mapping = {'4': 1000}

    def __init__(self, server, **kwargs):
        self.next_id = 1000
        self.config = kwargs
        self.name = kwargs.get('name','YouTube')

        self.login = kwargs.get('userid',kwargs.get('login',''))
        self.password = kwargs.get('password','')
        self.locale = kwargs.get('locale', None)
        self.quality = kwargs.get('quality','sd')
        self.showStandardFeeds = (kwargs.get('standard_feeds','True') in ['Yes','yes','true','True','1'])
        self.proxy_mode = kwargs.get('proxy_mode', 'redirect')
        self.cache_directory = kwargs.get('cache_directory', None)
        self.cache_maxsize = kwargs.get('cache_maxsize', 100000000)
        self.buffer_size = kwargs.get('buffer_size', 2000000)
        
        self.urlbase = kwargs.get('urlbase','')
        if( len(self.urlbase)>0 and
            self.urlbase[len(self.urlbase)-1] != '/'):
            self.urlbase += '/'

        self.server = server
        self.update_id = 0
        self.store = {}

        rootItem = Container(ROOT_CONTAINER_ID,self,-1, self.name)
        self.store[ROOT_CONTAINER_ID] = rootItem


        
        if (self.showStandardFeeds):
            standardfeeds_uri = 'http://gdata.youtube.com/feeds/api/standardfeeds'
            if self.locale is not None:
                standardfeeds_uri += "/%s" % self.locale
            standardfeeds_uri += "/%s"        
            self.appendFeed('Most Viewed', standardfeeds_uri % 'most_viewed', rootItem)
            self.appendFeed('Top Rated', standardfeeds_uri % 'top_rated', rootItem)
            self.appendFeed('Recently Featured', standardfeeds_uri % 'recently_featured', rootItem)
            self.appendFeed('Watch On Mobile', standardfeeds_uri % 'watch_on_mobile', rootItem)
            self.appendFeed('Most Discussed', standardfeeds_uri % 'most_discussed', rootItem)
            self.appendFeed('Top Favorites', standardfeeds_uri % 'top_favorites', rootItem)
            self.appendFeed('Most Linked', standardfeeds_uri % 'most_linked', rootItem)
            self.appendFeed('Most Responded', standardfeeds_uri % 'most_responded', rootItem)
            self.appendFeed('Most Recent', standardfeeds_uri % 'most_recent', rootItem)

        if len(self.login) > 0:
            userfeeds_uri = 'http://gdata.youtube.com/feeds/api/users/%s/%s'
            self.appendFeed('My Uploads', userfeeds_uri % (self.login,'uploads'), rootItem)
            self.appendFeed('My Favorites', userfeeds_uri % (self.login,'favorites'), rootItem)           
            playlistsItem = YoutubePlaylistContainer(MY_PLAYLISTS_CONTAINER_ID, self, rootItem.get_id(), 'My Playlists')
            self.storeItem(rootItem, playlistsItem, MY_PLAYLISTS_CONTAINER_ID)
            subscriptionsItem = YoutubeSubscriptionContainer(MY_SUBSCRIPTIONS_CONTAINER_ID, self, rootItem.get_id(), 'My Subscriptions')
            self.storeItem(rootItem, subscriptionsItem, MY_SUBSCRIPTIONS_CONTAINER_ID)
                      
        self.init_completed()


    def __repr__(self):
        return str(self.__class__).split('.')[-1]


    def storeItem(self, parent, item, id):
        self.store[id] = item
        parent.add_child(item)


    def appendFeed( self, name, feed_uri, parent):
        id = self.getnextID()
        item = YoutubeFeed(id, self, parent.get_id(), name, feed_uri)
        self.storeItem(parent, item, id)


    def appendVideoEntry(self, entry, parent):
        id = self.getnextID()
        external_id = entry.id.text.split('/')[-1]
        title = entry.media.title.text
        url = entry.media.player.url
        mimetype = MPEG4_MIMETYPE
        item = YoutubeVideoItem (self, parent, id, external_id, title, url, mimetype, entry)
        self.storeItem(parent, item, id)

    def len(self):
        return len(self.store)

    def get_by_id(self,id):
        if isinstance(id, basestring):
            id = id.split('@',1)
            id = id[0]
        try:
            return self.store[int(id)]
        except (ValueError,KeyError):
            pass
        return None

    def getnextID(self):
        ret = self.next_id
        self.next_id += 1
        return ret

    def upnp_init(self):
        self.current_connection_id = None

        if self.server:
            self.server.connection_manager_server.set_variable(0, 'SourceProtocolInfo',
                                                                    ['http-get:*:%s:*' % MPEG4_MIMETYPE],
                                                                    default=True)

        self.yt_service = YouTubeService()
        self.yt_service.client_id = 'ytapi-JeanMichelSizun-youtubebackendpl-ruabstu7-0'
        self.yt_service.developer_key = 'AI39si7dv2WWffH-s3pfvmw8fTND-cPWeqF1DOcZ8rwTgTPi4fheX7jjQXpn7SG61Ido0Zm_9gYR52TcGog9Pt3iG9Sa88-1yg'
        self.yt_service.email = self.login
        self.yt_service.password = self.password
        self.yt_service.source = 'Coherence UPnP backend'
        if len(self.login) > 0:
            d = threads.deferToThread(self.yt_service.ProgrammaticLogin)


    def retrieveFeedItems (self, parent, feed_uri):
        feed = threads.deferToThread(self.yt_service.GetYouTubeVideoFeed,feed_uri)

        def gotFeed(feed):
           if feed is None:
               print "Unable to retrieve feed %s" % feed_uri
               return
           for entry in feed.entry:
               self.appendVideoEntry(entry, parent)

        def gotError(error):
            print "ERROR: %s" % error

        feed.addCallbacks(gotFeed, gotError)
        return feed

    def retrievePlaylistFeedItems (self, parent, playlist_id):
              
        feed = threads.deferToThread(self.yt_service.GetYouTubePlaylistVideoFeed,playlist_id=playlist_id)       
        def gotFeed(feed):
           if feed is None:
               print "Unable to retrieve playlist items %s" % feed_uri
               return
           for entry in feed.entry:
               self.appendVideoEntry(entry, parent)

        def gotError(error):
            print "ERROR: %s" % error

        feed.addCallbacks(gotFeed, gotError)
        return feed

    def retrieveSubscriptionFeedItems (self, parent, uri):              
        entry = threads.deferToThread(self.yt_service.GetYouTubeSubscriptionEntry,uri)

        def gotEntry(entry):
           if entry is None:
               print "Unable to retrieve subscription items %s" % uri
               return
           feed_uri = entry.feed_link[0].href
           return self.retrieveFeedItems(parent, feed_uri)

        def gotError(error):
            print "ERROR: %s" % error
        entry.addCallbacks(gotEntry, gotError)
        return entry

    def retrievePlaylistFeeds(self, parent):
        playlists_feed = threads.deferToThread(self.yt_service.GetYouTubePlaylistFeed, username=self.login)

        def gotPlaylists(playlist_video_feed):
           if playlist_video_feed is None:
               print "Unable to retrieve playlists feed"
               return
           for playlist_video_entry in playlist_video_feed.entry:
               title = playlist_video_entry.title.text
               playlist_id = playlist_video_entry.id.text.split("/")[-1] # FIXME find better way to retrieve the playlist ID
               id = self.getnextID()
               item = YoutubePlaylistFeed(id, self, parent.get_id(), title, playlist_id)
               self.storeItem(parent, item, id)               

        def gotError(error):
            print "ERROR: %s" % error

        playlists_feed.addCallbacks(gotPlaylists, gotError)
        return playlists_feed


    def retrieveSubscriptionFeeds(self, parent):
        playlists_feed = threads.deferToThread(self.yt_service.GetYouTubeSubscriptionFeed, username=self.login)
 
        def gotPlaylists(playlist_video_feed):
           if playlist_video_feed is None:
               print "Unable to retrieve subscriptions feed"
               return
           for entry in playlist_video_feed.entry:
               type = entry.GetSubscriptionType()
               title = entry.title.text
               uri = entry.id.text
               name = "[%s] %s" % (type,title)
               id = self.getnextID()
               item = YoutubeSubscriptionFeed(id, self, parent.get_id(), name, uri)
               self.storeItem(parent, item, id)              

        def gotError(error):
            print "ERROR: %s" % error

        playlists_feed.addCallbacks(gotPlaylists, gotError)
        return playlists_feed